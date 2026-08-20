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

(Strategy workbooks imported via Module 8 may also reference Euribor,
an Australian exchange market, and SARON 3M futures — these are
recognized by name but have no configured `MarketDefinition` and are
not tradable/scannable through LSEG or any other provider today; see
Module 8 and the Development Roadmap's Deferred list below.)

The application lets a trader construct multi-leg strategies, scan
combinations of futures contracts, identify strategies exhibiting
range-bound behaviour, and inspect their historical behaviour.

The interface is built with Streamlit (`ui/` — Modules 6A/6B, see below).

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

**Live LSEG testing findings (market-data correctness patch):**

- **RIC conventions per market** (`core.config.MARKETS`, all handled
  generically by `core.ric.build_ric()` -- root + expiry-month code +
  `ric_year_digits`-sliced year, no per-market branching):
  - SOFR: root `SRA`, 2-digit year -- e.g. `SRAU26`. Confirmed working
    live; `TRDPRC_1`/OHLC/`SETTLE` all populated.
  - FED_FUNDS: root `FF`, 2-digit year -- e.g. `FFU26`. Confirmed
    working live; same full field population as SOFR.
  - SONIA: root `SON` (corrected from `SFI`), 1-digit year -- e.g.
    `SONU6`. Confirmed working live at `DAILY`, but see the field-
    population note below.
  - CORRA: root `CRA`, 1-digit year -- e.g. `CRAU6`/`CRAH7`. RIC
    construction is correct and unchanged. The current LSEG account
    lacks entitlement for this universe (`TS.Interday.
    UserNotPermission.70112`, "User does not have permission for this
    universe") -- **a permissions issue, not an Oscill8 RIC bug.** Do
    not alter CORRA's root/year-digit convention or add fallback
    identifiers to work around this.
  - ESTR (€STR): root `SRE` (corrected from `ESR`), 2-digit year
    (corrected from 1) -- e.g. `SREU26`. The old `ESRU6`-style
    convention returned LSEG error 70005 ("The universe is not
    found"). Confirmed working live: `TRDPRC_1`/OHLC/`SETTLE`/bid/ask
    all populated.
- **DAILY-only SETTLE fallback for Close** (`core.downloader.
  _normalize_columns`): live testing found SONIA's `DAILY` response has
  `TRDPRC_1`/`OPEN_PRC`/`HIGH_1`/`LOW_1`/bid/ask entirely NA, while
  `SETTLE` carries the real daily price -- previously discarded
  entirely during canonicalization (not in `_CANONICAL_COLUMNS`),
  producing an all-NaN `Close`. `_normalize_columns` now accepts
  `settle_fallback_for_close` (set only when the top-level requested
  interval is `DAILY`, i.e. `native_interval == "daily"` in
  `_fetch_chunk` -- `FOUR_HOUR` also resolves to a native `"hourly"`
  fetch, so it's correctly excluded too); when set, a `SETTLE` column
  present in the raw response row-wise fills only the NaN gaps left in
  `Close` after the primary alias (`TRDPRC_1`/`CLOSE`/`CLOSE_1`) is
  coerced to numeric -- `Close = Close.fillna(SETTLE)`, never a global
  replacement. SOFR/Fed Funds/€STR have both `TRDPRC_1` and `SETTLE`
  populated (and they can differ slightly) -- their existing
  `TRDPRC_1`-derived `Close` is completely unaffected. Open/High/Low
  are never fabricated from `SETTLE`. Intraday (`HOURLY`) semantics are
  unchanged -- `SETTLE` is never consulted there.

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
- Test suite: 139/139 passing (97 pre-existing + 42 new) locally with
  the pinned `requirements.txt` versions installed.

## Module 4A – Range-Bound Analytics

COMPLETED AND TESTED.

Consumes a `strategy_engine.StrategyHistory` and computes independently-
interpretable range/location, movement, oscillation, and mean-reversion
diagnostics for a selected historical window. Never retrieves market
data itself; never imports `database` or `core.downloader`.

range_analytics/
    __init__.py       (public re-exports)
    lookback.py        (resolve_window -- lookback N valid observations
                        OR calendar start/end, never both; drops NaN
                        Strategy rows before applying N)
    location.py         (mean/median, full & robust range bounds/width/
                        position, distance-from-mean, z-score)
    volatility.py        (realized_volatility -- sample stdev, ddof=1, of
                        level changes; not annualized, not % returns)
    efficiency.py         (Kaufman-style efficiency_ratio)
    oscillation.py         (count_crossings -- hysteresis-band crossing count)
    mean_reversion.py       (AR1Fit, fit_ar1 -- differenced-OLS AR(1), half-life)
    units.py                 (price_to_bp -- per-market bp_per_point conversion)
    results.py                (RangeAnalytics dataclass, analyze_range() entry point)

tests/
    test_range_lookback.py, test_range_location.py, test_range_volatility.py,
    test_range_efficiency.py, test_range_oscillation.py,
    test_range_mean_reversion.py, test_range_units.py, test_range_analytics.py

Key design points a future session needs:

- **Robust range bounds are HARD-CODED at P5/P95**
  (`series.quantile(0.05)` / `series.quantile(0.95)` in `location.py`) —
  there is no configurable percentile parameter today. A future
  "configurable percentile band" feature (5/95, 10/90, 25/75, ...) is
  under consideration but **not implemented, not started, and not
  approved** — it would touch `location.py`'s `range_low_robust`/
  `range_high_robust`/`range_width_robust` plus `RangeAnalytics`/
  `analyze_range()`'s signature.
- `range_position` (full and robust) is deliberately **not clipped to
  [0, 1]** — a value outside `[low, high]` is itself meaningful (current
  sits below the historical low, or above the P95) and is surfaced as
  such everywhere downstream, including the UI.
- `efficiency_ratio`, `realized_volatility`, `count_crossings`, `fit_ar1`
  are all pure functions of an already NaN-free, already-window-resolved
  `pd.Series` — `resolve_window()` is the one place NaN-dropping/window
  selection happens; the diagnostic functions never re-implement it.
- AR(1) is fit on level **changes** (`ΔS_t = α + γ·S_(t-1) + ε_t`), not
  raw levels — algebraically equivalent to `S_t = α + β·S_(t-1) + ε_t`
  via `beta = 1 + gamma`. `gamma` is what a hand-rolled OLS actually fits
  (no statsmodels/scipy dependency); `beta` is derived and reported
  because its sign/magnitude directly answers smooth (`0<beta<1`) vs.
  oscillatory (`-1<beta<0`) vs. random-walk (`beta==1`) vs.
  non-mean-reverting (`|beta|>=1`) reversion.
- `half_life = ln(2) / (-ln(|beta|))` for `0 < |beta| < 1`; `0.0` at
  `beta == 0` (computed directly, no `log(0)` singularity); `NaN` for
  `|beta| >= 1`.
- The only "dislocation from equilibrium" field that exists is
  `z_score` (`(current - mean) / std`, same window). A separate,
  **unapproved** future "robust Z-score to distinguish range-boundedness
  quality from current dislocation" is **not implemented** and is not
  the same thing as this field — see the Development Roadmap below.
- Test suite (current file-level counts): `test_range_lookback.py` 10,
  `test_range_location.py` 12, `test_range_volatility.py` 5,
  `test_range_efficiency.py` 6, `test_range_oscillation.py` 14,
  `test_range_mean_reversion.py` 10, `test_range_units.py` 7,
  `test_range_analytics.py` 10 — 74 tests total for this module.

## Module 4B – Multi-Lookback / Stability Analytics

COMPLETED AND TESTED.

Repeatedly analyzes ONE `StrategyHistory` at MULTIPLE lookback windows
and describes how Module 4A's own measurements move across them
(dispersion, short-vs-long change, step structure) — built entirely on
top of `analyze_range()`, never reaching into 4A's lower-level
primitives directly. This guarantees 4B can never drift from 4A's own
edge-case/NaN handling.

range_analytics/
    multi_lookback.py  (MultiLookbackAnalytics, analyze_multi_lookback();
                        also range_to_volatility_ratio, robust_to_full_width_ratio)
    stability.py        (LookbackStability, build_stability() -- generic
                        dispersion/change summary of a metric's values
                        across lookbacks)

tests/
    test_range_stability.py, test_range_multi_lookback.py

Key design points a future session needs:

- `lookbacks` must be strictly increasing, caller-supplied, arbitrary
  length >= 1 — the typical default `(20, 40, 60, 90, 120)` is
  illustrative, never hard-coded logic anywhere downstream.
- `crossing_equilibrium=None` (default) means each lookback window gets
  its **own** median as equilibrium, not one global value forced across
  differently-sized windows.
- `LookbackStability.signed` (declared per metric, e.g. `ar1_beta` is
  signed) suppresses `short_vs_long_ratio`/`pairwise_ratios` for metrics
  where a ratio across a sign change would be meaningless — this is a
  fixed property of the metric, never inferred from whether a particular
  instance's values happened to be positive.
- Ten metrics get a `*_stability` field on `MultiLookbackAnalytics`:
  `range_width_robust`, `range_low_robust`, `range_high_robust`,
  `median`, `realized_vol_bp`, `efficiency_ratio`,
  `normalized_crossing_frequency`, `ar1_beta`, `half_life`,
  `range_to_volatility_ratio`. Purely descriptive — no stability
  verdict, classification, or score.
- Test suite (current file-level counts): `test_range_stability.py` 9,
  `test_range_multi_lookback.py` 21 — 30 tests total for this module.

## Module 5A – Template / Candidate Universe Engine

COMPLETED AND TESTED.

Translates grid-style dense weight-vector templates into
`strategy_engine.StrategyDefinition` and rolls them across a market's
contract curve into a deduplicated candidate universe of
`StrategyInstance`s. No market data fetched here, no Module 4 analytics
computed here — this module produces candidate `StrategyInstance`s only.

template_scanner/
    templates.py   (template_from_dense_weights() -- dense vector -> StrategyDefinition)
    universe.py     (generate_candidates(), generate_candidate_universe(),
                    dedupe_candidates() -- max_curve_position/eligible_rics
                    post-filters, deterministic dedup)

tests/
    test_template_scanner_templates.py, test_template_scanner_universe.py

Key design points a future session needs:

- `template_from_dense_weights()` is a pure format conversion — dense
  grid position -> sparse offsets/weights (leading zeros re-based away,
  interior zeros become gaps) — never a new engine; all shape validation
  is `StrategyDefinition`'s own.
- Weights are **never normalized**: `(2, -4, 2)` stays `(2, -4, 2)`,
  distinct from `(1, -2, 1)` — different economic exposure, same shape.
- `dedupe_candidates()` treats two candidates as identical iff market,
  RICs, weights (exact, unscaled), interval, and price_field all match —
  `(1,-2,1)` and `(2,-4,2)` on the same RICs are NOT duplicates.
- Same-market templates only — an intermarket template (legs spanning
  multiple markets) is deferred as a materially different combinatorial
  problem, expected to be an additive sibling module, not a
  modification of this one.
- Test suite (current file-level counts):
  `test_template_scanner_templates.py` 14,
  `test_template_scanner_universe.py` 15 — 29 tests total for this
  module.

## Module 5B – Scanner Orchestration, Filtering, Ranking

COMPLETED AND TESTED. Includes the 5B.1 hardening pass and the
metric-resolution unification fix described below.

Prices a candidate universe through `strategy_engine` (one shared leg
cache per scan) and measures each resulting history through
`range_analytics`, then offers separate, optional filtering and
transparent multi-key ranking over the results — never a composite/
opaque score, never a hard-coded threshold.

template_scanner/
    scanner.py       (ScanRequest, ScanReport, SkippedCandidate, run_scan(),
                     analyze_histories() -- the mode-agnostic core taking
                     already-built StrategyHistory objects)
    filters.py         (FilterCriterion, apply_filters(), at_lookback(),
                     stability() -- accessor factories)
    ranking.py           (SortKey, rank_results() -- stable multi-key sort, NaN last)
    scan_results.py        (ScanCandidateResult, results_to_dataframe() --
                     curated scalar column subset)
    metrics.py                (at_lookback(), metric_value(), the canonical
                     direct-field/derived-metric resolver)

tests/
    test_template_scanner_scanner.py, test_template_scanner_filters.py,
    test_template_scanner_ranking.py, test_template_scanner_scan_results.py,
    test_template_scanner_metrics.py

Key design points a future session needs:

- **Unavailable-market-data hardening (5B.1)**: `run_scan()` catches
  exactly ONE typed exception around `build_history()` —
  `core.downloader.MarketDataUnavailableError`, LSEG's own confirmation
  (error code `TS.Interday.UserRequestError.70005`, message "The
  universe is not found") that a RIC has no market data at all. The
  affected candidate is skipped and recorded on `ScanReport.skipped`;
  the scan continues. A confirmed-unavailable RIC is remembered
  (`unavailable_rics`) for the rest of that scan so later candidates
  referencing it are skipped without a repeat LSEG attempt. Every other
  exception (network/session/auth/vendor errors, database errors,
  programming bugs, analytics errors) is **not caught** and propagates,
  aborting the scan — this is deliberately narrow and must never grow
  into a general failure bucket. No-data / short-history (a valid RIC
  with nothing in the requested date range) is NOT an exception at all —
  it flows through as empty/NaN-heavy results, Modules 1–4's existing,
  tested behavior.
- **Canonical metric resolution (metric-resolution fix)**:
  `metrics.metric_value()` is the single resolver for "a scalar metric
  by name on a RangeAnalytics" — either a direct dataclass field (e.g.
  `efficiency_ratio`) or one of the derived metrics
  (`normalized_crossing_frequency`, `range_to_volatility_ratio`,
  `robust_to_full_width_ratio`). `results_to_dataframe()` and
  `filters.at_lookback()` both resolve through this one function, so a
  metric name means the same thing in the scanner result table and in
  filter/rank accessors — this is what lets a UI filter/rank on a
  derived metric like `normalized_crossing_frequency`, not just a raw
  `RangeAnalytics` field.
- `ScanRequest.contract_start`/`contract_end` (which rolling combinations
  get generated) and `price_start`/`price_end` (what date range gets
  priced) stay independent parameters, matching Module 3's own
  contract-selection-vs-price-history separation.
- `FilterCriterion.passes()`: NaN always fails (never silently passes);
  `apply_filters()` with an empty criteria list returns every candidate
  unfiltered — filtering is opt-in, never a default threshold.
  `SortKey`: NaN always sorts last regardless of direction; no keys
  supplied returns candidates in original order.
- Test suite (current file-level counts):
  `test_template_scanner_scanner.py` 19,
  `test_template_scanner_filters.py` 14,
  `test_template_scanner_ranking.py` 9,
  `test_template_scanner_scan_results.py` 11,
  `test_template_scanner_metrics.py` 10 — 63 tests total for this
  module (hardening and metric-resolution tests are included within
  `test_template_scanner_scanner.py`/`filters.py`/`metrics.py` above,
  not split into separate files).

## Module 6A – Streamlit Range-Bound Scanner UI

COMPLETED AND TESTED. Shipped in two passes: an initial minimal
functional scanner, then a compact trader-facing redesign (grouped
scan panel, curve-position strategy grid replacing free-text ratio
entry, results made the dominant visual section, Ranking/Filters
popovers, tooltips).

ui/
    __init__.py     (package docstring / file-responsibility map)
    app.py           (entry point / page orchestration -- `streamlit run ui/app.py`)
    state.py          (session-state keys: expensive scan result +
                     selected-candidate/history cache -- see Module 6B)
    controls.py        (scan panel: market/interval/dates/lookbacks/Run Scan;
                     strategy grid: curve positions as columns, one row
                     per template)
    scan_view.py         (Run Scan: builds ScanRequest, calls run_scan()
                     exactly once per press, UI-boundary error handling)
    results_view.py        ("Range-Bound Opportunities": status, ranking/
                     filters popovers, ranked result grid, row ->
                     ScanCandidateResult selection, Selected Strategy
                     summary panel, skipped-candidates expander)
    formatting.py            (pure helpers: strategy-grid-row ->
                     StrategyDefinition translation, filter/sort-key
                     construction from template_scanner's own accessor
                     factories, ranked-by/rank-column/selection formatting
                     -- zero Streamlit import, fully unit-testable)

tests/
    test_ui_formatting.py, test_ui_controls.py

Key design points a future session needs:

- **The UI is a thin layer, structurally.** It never computes analytics,
  never duplicates filtering/ranking/derived-metric formulas, never
  imports `lseg.data`. `formatting.py` has zero Streamlit dependency by
  design — every filter/sort accessor it builds is a closure over
  `template_scanner.filters.at_lookback()`/`stability()`, never a
  reimplementation.
- **Pipeline order matters**: filter and rank the Python
  `ScanCandidateResult` objects FIRST (`apply_filters` -> `rank_results`),
  THEN build the display DataFrame (`results_to_dataframe` ->
  `to_display_dataframe` -> `add_rank_column`) — row position in the
  displayed grid must map back to the same-position entry in the ranked
  candidate list, since row-selection in `st.dataframe` only gives back
  a positional index.
- **Strategy grid is position-relative, not real-contract**: columns are
  bare curve-position numbers (`1, 2, 3, ...`), never real RIC codes —
  `template_from_dense_weights()` + `generate_instances()` roll a shape
  across every eligible starting point in the contract universe, so
  "position 1" is a different real contract for each rolled candidate.
  Showing one fixed row of contract codes as column headers would
  misrepresent this; see `ui.formatting.CURVE_POSITION_HELP`.
- **Grid cells are `TextColumn`, not `NumberColumn`** (with a
  `validate=r"^-?\d*\.?\d*$"` regex), a deliberate, empirically-verified
  workaround: this Streamlit build (1.60.0) renders an unpopulated/NaN
  `NumberColumn` cell as the literal text `"None"` regardless of dtype
  (`float64` NaN, Python `None`, pandas nullable `Float64`+`pd.NA` all
  reproduce it), while `TextColumn` with an empty string renders
  correctly blank. `ui.formatting._cell_to_float()` parses the resulting
  strings, treating blank text and an incomplete mid-edit token (a lone
  `"-"` or `"."`) as 0 — same as an explicit 0 (skip this position).
- **`Ranking ▾`/`Filters ▾` use `st.popover`**, not `st.expander` —
  floating panels that don't push the results table down. The "Ranked
  by: ..." label is built by reading the Ranking popover's persisted
  `st.session_state` values BEFORE that popover's own widgets are
  (re)created later in the same script pass (`_current_rank_state()` in
  `results_view.py`) — safe because nothing changes state between that
  read and the widgets' own creation in the same rerun.
- No new backend capability was added to support this module — every
  control maps onto an existing `template_scanner`/`strategy_engine`
  public function.
- Test suite (current file-level counts): `test_ui_formatting.py` 30,
  `test_ui_controls.py` 3 — 33 tests total for this module (the pure
  `_default_grid()` grid-construction helper in `controls.py` is the
  only piece of that file that's directly unit-testable; the rest
  renders Streamlit widgets directly and is exercised via manual/browser
  smoke testing instead, not brittle Streamlit-rendering unit tests).

## Module 6B – Selected-Strategy History Chart

COMPLETED AND TESTED.

Adds the Selected Strategy history chart (Plotly) immediately below the
Selected Strategy summary panel — the most important new visual
component after the scanner grid itself.

ui/
    chart_view.py   (get_selected_history(), build_strategy_chart(),
                    render_chart())

tests/
    test_ui_chart.py

Key design points a future session needs:

- **No backend change was needed or made.** `ScanCandidateResult` does
  NOT retain the raw `StrategyHistory.history` DataFrame (only
  `instance` + the already-computed `MultiLookbackAnalytics`) — the
  chart re-fetches it for the SELECTED candidate only via the existing,
  public `strategy_engine.pricing.build_history(candidate.instance,
  scan_request.price_start, scan_request.price_end)`. Since that exact
  RIC/interval/price-window combination was already fetched during the
  scan just run, this is a `database.get_history()` **SQLite cache
  hit** — verified empirically (instrumented call-counting harness) to
  add zero LSEG calls beyond Module 2's own pre-existing, documented
  "today's still-open bar is never cached, always re-fetched" behavior,
  which is unrelated to this module and would occur regardless of who
  calls `get_history()`.
- The fetched history is cached in `ui.state` per selection
  (`SELECTED_HISTORY`, invalidated only when `set_selected_candidate()`
  sees the candidate's object identity actually change) — fetched at
  most once per row click, never once per rerun.
- **Overlay levels (Robust Low/Median/Robust High) are never
  recomputed** — read directly from the candidate's already-computed
  `MultiLookbackAnalytics.per_lookback` via
  `template_scanner.metrics.at_lookback()`, the exact same resolver the
  result table and Selected Strategy panel use.
- **The plotted window is sliced with `range_analytics.lookback.
  resolve_window()`** — the SAME function Module 4A uses internally —
  so the plotted observations are always exactly the ones the displayed
  overlay levels were computed from, for whichever lookback the Chart
  Horizon selector currently holds.
- **Chart Horizon selector** (`st.segmented_control`) is restricted to
  whichever lookbacks the ORIGINAL scan actually requested
  (`scan_request.lookbacks`) — switching it re-reads a different
  already-computed `RangeAnalytics` and re-slices the already-cached
  history; verified (same instrumented harness) to add zero additional
  calls of any kind.
- One primary chart only, by design: strategy price line, Robust Low/
  Median/Robust High overlay lines + shaded band between Low/High, a
  marker on the latest observation, `plotly_dark` theme, no legend
  clutter, unified hover. No secondary diagnostic charts (AR(1), crossing
  markers, rolling-stability, z-score) — deferred, see the Development
  Roadmap below.
- Test suite (current file-level count): `test_ui_chart.py` 14 tests —
  the pure `build_strategy_chart()` figure builder is unit-tested
  (trace/shape structure, exact lookback-window sizing, empty-window
  handling, and — added in the trading-day data-integrity pass below —
  rangebreak/weekend/holiday chart-axis behaviour);
  `get_selected_history()`'s I/O and `render_chart()`'s widget
  rendering are exercised via manual/browser smoke testing.

---

## Data-Integrity Pass — Trading-Day / Valid-Observation Handling

COMPLETED AND TESTED. A cross-cutting audit + targeted fix, not a new
numbered module — it touches Module 3 (`strategy_engine/pricing.py`)
and Module 6B (`ui/chart_view.py`) only.

**Canonical invariant (applies pipeline-wide, from Module 1 through the
chart):**

> Oscill8 DAILY analytics operate on valid market observations, not
> calendar days.

Concretely: a weekend, a holiday, or any other date a market simply has
no bar for is never a row anywhere in the pipeline — not in LSEG's own
response, not in the SQLite cache, not in a `StrategyHistory.history`
frame, not in an analysis window. It is absent, never a zero, never
forward-filled, never interpolated. `range_analytics.lookback.
resolve_window`'s `lookback=N` therefore already meant, and continues
to mean, N *valid* observations — audited and confirmed end-to-end
before any code changed; no `range_analytics` calculation needed to
change (see `tests/test_trading_day_regression.py` for the end-to-end
proof: a mocked history spanning a real weekend, run through
`strategy_engine.pricing.build_history` -> `range_analytics.results.
analyze_range`, matches hand-computed values treating Friday->Monday as
consecutive observations).

**Multi-leg invariant** (`strategy_engine/pricing.py`):

> A synthetic Strategy observation exists only when every required leg
> has a valid price on that date.

`build_history` already aligned legs with an inner join on `Date` (no
forward-fill), but the join key itself didn't previously exclude a Date
where a leg had a row with a NaN `price_field` value (a vendor data-
quality gap on an otherwise-normal trading date) — that NaN would enter
the joined frame and only get dropped later, by whichever caller
happened to run `resolve_window` afterward. Hardened: each leg's
NaN-`price_field` rows are now dropped immediately after fetch, before
the join, so `StrategyHistory.history` is NaN-free at its own boundary,
not by accident of a downstream caller's behaviour. A missing Date and
a NaN-priced Date are now handled identically at the same point in the
pipeline. This is deliberately not solved by an explicit per-market
holiday calendar (CME/ICE/SONIA/CORRA/€STR, etc.) — none was added or
is needed. Each leg's own valid-observation series (from
`database.get_history`) is the sole source of truth, which is what lets
this generalize unchanged to a future intermarket strategy whose legs
may follow different market holiday calendars: the inner join still
just takes the intersection of whatever dates each leg actually has.

**Chart axis fix** (`ui/chart_view.py`): the analytical dataframe
plotted by `build_strategy_chart` was already gap-free (no weekend/
holiday row), so the line trace itself always connected Friday directly
to Monday — but Plotly's default continuous date axis still reserved
real calendar-time width for the non-trading days in between, which
read visually as a gap/whitespace even though no data was missing. Two
new pure helpers close that up via Plotly `rangebreaks`, with no static
holiday calendar:
  - `_missing_weekdays(dates)`: any Mon-Fri weekday within
    `[dates.min(), dates.max()]` absent from `dates` — a holiday, or
    any other date this specific strategy's own valid-observation
    series happens to lack. Computed dynamically from the actual
    plotted series every time, never from a maintained list. Dates are
    `.normalize()`-d before comparison so a DAILY timestamp's
    time-of-day component can never cause a spurious mismatch.
  - `_build_rangebreaks(dates)`: `[dict(bounds=["sat", "mon"])]`
    (always present — the fixed, market-agnostic weekend rule) plus a
    `dict(values=[...])` entry ONLY when `_missing_weekdays` finds
    something — a pure weekend-only gap never gets a redundant explicit
    Saturday/Sunday `values` list.
  Real chronological date labels are unaffected — rangebreaks only
  close up unused axis space, never relabel, reorder, or drop a plotted
  point (verified in `tests/test_ui_chart.py`).

**Deferred, not solved here:**
- Intraday (HOURLY/4H) non-trading-hour chart gaps — `rangebreaks`
  would additionally need hour-of-day bounds; out of scope, this pass
  is DAILY-focused only.
- Distinguishing a legitimate holiday from an unexpected vendor
  data-quality gap — both are treated identically today ("no valid
  price = no observation"); Oscill8 has no independent trading-holiday
  calendar to cross-check against, and LSEG is the documented sole
  source of truth. A future sparse-data/vendor-quality diagnostic
  (surfacing an unusually low hit-rate against business days) is a
  separate, unapproved idea, not implemented.

Test suite additions: `tests/test_strategy_pricing.py` (+3: NaN-priced
leg dropped before the join, intersection with a mixed missing-date/
NaN-price scenario, no forward-fill), `tests/test_ui_chart.py` (+9:
`_missing_weekdays`/`_build_rangebreaks` unit coverage plus chart-level
rangebreak/label-preservation checks), and a new
`tests/test_trading_day_regression.py` (+5: one continuous
downloader-mock -> `build_history` -> `analyze_range` chain over a real
weekend-spanning series).

---

## Module 7A – Strategy Set Engine

COMPLETED AND TESTED. A domain-modelling and backend-architecture
module, not analytics and not UI — nothing under `range_analytics/`
was touched, and no Streamlit code was added.

Introduces user-owned, named, serializable collections of strategy
definitions ("Strategy Sets" — e.g. "Churning", "6M Strategies",
"Medium Vol") that expand into the existing `strategy_engine.
StrategyInstance` architecture. The scanner (`template_scanner/`)
requires zero modifications and never imports this package.

strategy_sets/
    __init__.py     (public re-exports + design-principle summary)
    model.py          (StrategySet, StrategySetEntry, ExpansionSettings)
    serialization.py   (StrategySet <-> dict <-> JSON, no filesystem I/O)
    repository.py        (StrategySetRepository — save/load/list/
                        duplicate/rename/delete, one JSON file per set)
    expansion.py            (expand_strategy_set() -> StrategyInstance[])

tests/
    test_strategy_sets_model.py, test_strategy_sets_serialization.py,
    test_strategy_sets_expansion.py, test_strategy_sets_repository.py

Key design points a future session needs:

- **Object model**: a `StrategySet` is a named, validated tuple of
  `StrategySetEntry`. Each entry composes the existing, unmodified
  `strategy_engine.StrategyDefinition` (market_key/offsets/weights/
  interval/price_field) rather than duplicating its shape/validation,
  plus a human-facing `name`, an `enabled` flag, and `expansion`
  (`ExpansionSettings` — `max_curve_position`/`eligible_rics` only).
  Deliberately **not** a second class literally named
  `StrategyDefinition`: that name is already `strategy_engine.
  definitions.StrategyDefinition` project-wide, and reusing it for a
  materially different, richer object would make "StrategyDefinition"
  ambiguous depending on which module you're reading.
- **The contract-selection window is NOT part of the saved object**
  (design correction made during review — the first draft put
  `contract_start`/`contract_end` on `ExpansionSettings` per-entry;
  this was reverted). `expand_strategy_set(strategy_set, contract_start,
  contract_end, only_enabled=True, dedupe=True)` takes the window as a
  call-time argument, shared across every entry in one call — exactly
  matching `template_scanner.scanner.ScanRequest`, which already
  carries one contract window shared across its whole
  `list[StrategyDefinition]`. Rationale: a Strategy Set describes
  *what* to scan, not *when* — baking an absolute date range into a
  *saved, reused* object goes stale the moment "today" moves past it,
  which would have undermined the module's own reusability goal and
  diverged from `ScanRequest`'s established shared-window precedent for
  no compensating benefit. `max_curve_position`/`eligible_rics` stayed
  per-entry deliberately: those are strategy-shape/liquidity-dependent
  (a 12-leg curve and a 3-leg fly, or two different markets, can
  legitimately want different curve-position/eligibility filters even
  under one shared scan window) — not a calendar concept, so staleness
  doesn't apply to them.
- **Expansion reuses `template_scanner.universe` unchanged** —
  `generate_candidates()` per enabled entry (under the shared window),
  combined and (by default) `dedupe_candidates()`-ed across the whole
  set. No new rolling/filtering/dedup logic exists in this package.
  Audit note: the scanner's actual entry point today
  (`run_scan(ScanRequest)`) doesn't literally accept
  `StrategyInstance[]` as an argument — it takes
  `list[StrategyDefinition]` + one shared window and performs this same
  `generate_candidates`/`dedupe_candidates` step internally. This
  module's output is byte-identical in type/shape to what `run_scan()`
  already builds internally, so it's usable anywhere a manually-built
  candidate list is today without changing `scanner.py`/`ScanRequest`.
  Wiring `StrategySet` output *into* a running scan is an explicit
  non-goal of this phase.
- **JSON schema** (readable/indented, one file per set, filename =
  `<name>.json`): top-level `schema_version`/`name`/`description`/
  `entries`; each entry flattens the `StrategyDefinition`'s four shape
  fields directly onto itself (not nested under a separate
  `"definition"` key) for easier hand-editing. No contract window
  appears anywhere in a saved file.
- **Validation**: `StrategyDefinition`'s own validation (unknown
  market, non-increasing offsets, all-zero weights, unsupported
  interval/price_field) runs unmodified on every entry. `StrategySet`
  additionally requires ≥1 entry and unique entry `name`s within one
  set. `StrategySetRepository` filenames rely on `StrategySet.name`'s
  own validation already being filesystem-safe (letters/digits/
  spaces/`-`/`_` only) — no separate slugification step exists.
  Deserialization errors distinguish a genuinely missing JSON key (a
  distinct `ValueError`) from a downstream domain-validation failure
  (propagated unmodified, e.g. `StrategyDefinition`'s own `KeyError` for
  an unknown market) — the dict-lookup guard wraps only the lookup
  itself, never the object construction that follows it.
- **Repository**: directory creation deferred to the first `save()`
  call (matches `database/connection.py`'s convention); `save()` is an
  upsert (overwrites silently); `duplicate()`/`rename()` both raise
  `FileExistsError` on a target-name collision rather than silently
  overwriting, and leave the source untouched on any failure.
- Test suite (current file-level counts): `test_strategy_sets_model.py`
  35, `test_strategy_sets_serialization.py` 41,
  `test_strategy_sets_expansion.py` 17,
  `test_strategy_sets_repository.py` 21 — 114 tests total for this
  module.

**Deferred, not solved here** (see the Development Roadmap below): true
intermarket (cross-market-leg) strategies, watchlists, alerts,
deployment. (A Strategy Set selector/editor UI was added afterward —
see Module 7B below — and wiring `expand_strategy_set()`/
`run_scan_on_instances()` into a running scan was added later still,
as a separate, additive execution path — see Module 9. Neither
retroactively changes anything documented above; this module's own
scope was, and remains, the domain model/persistence/expansion layer
only.)

## Module 7B – Strategy Set UI (Grid Unification)

COMPLETED AND TESTED. Predates Module 8/9 below; recorded here because
it was not previously documented in this file even though it shipped
and has been in continuous use.

Integrates the Strategy Set selector directly into the existing
Strategy Templates grid rather than adding a second table/section:
*"Strategy Templates is the working strategy grid; a Strategy Set is
simply a saved named version of that grid."* There is exactly one
strategy grid and one Run Scan button — a loaded Strategy Set becomes
ordinary grid rows and is run exactly like manually-typed rows.

ui/
    strategy_set_state.py        (session-state: which saved set, if
                                  any, is loaded; the pending-selection
                                  indirection its widget-lifecycle fix
                                  depends on — no separate "draft"
                                  state, the grid itself is the draft)
    strategy_set_formatting.py   (pure helpers: StrategySet entries <->
                                  grid rows, no Streamlit import)
    strategy_set_view.py         (the selector + Save/"+ New"/Delete
                                  controls rendered inside ui.controls'
                                  Strategy Templates section; Delete
                                  requires an explicit confirmation
                                  dialog)

tests/
    test_ui_strategy_set_formatting.py, test_ui_strategy_set_state.py,
    test_ui_strategy_set_selector_lifecycle.py,
    test_ui_strategy_set_multimarket_roundtrip.py

Key design points a future session needs:

- **Per-row Market/Interval on the grid, not one grid-wide value**: the
  grid's own `Market`/`Interval` `SelectboxColumn`s let different rows
  belong to different markets/intervals in the same grid — required for
  a Strategy Set mixing markets (e.g. SOFR + SONIA + CORRA entries) to
  round-trip through load → edit → save → reload without any entry's
  market/interval silently normalizing to one value. `ui.formatting.
  build_definitions_from_grid()` resolves each row's own Market/
  Interval first, falling back to the scan bar's selectors only for a
  row that doesn't carry them (e.g. a brand-new blank row).
- **Selector widget-lifecycle**: Streamlit forbids writing to a
  widget's own session-state key after that widget has already been
  instantiated in the current script run. Save/`+ New`/Delete all need
  to change the selector's value from further down the same script
  pass — solved via a `PENDING_SELECTION` indirection
  (`ui.strategy_set_state`) that `render_selector()` applies to the
  widget's key on the *next* rerun, before the widget is re-created.
- **No separate Strategy-Set-specific scan execution path** here —
  `run_scan_on_instances()` is unused by this module; a loaded set
  takes the exact same `build_definitions_from_grid()` →
  `ScanRequest` → `run_scan()` path a manually-typed row does. See
  Module 9 for the separate, additive Strategy Set Scan path that
  *does* call `expand_strategy_set()`/`run_scan_on_instances()`.
- Test suite (current file-level counts): `test_ui_strategy_set_
  formatting.py` 14, `test_ui_strategy_set_state.py` 8, `test_ui_
  strategy_set_selector_lifecycle.py` 24, `test_ui_strategy_set_
  multimarket_roundtrip.py` 4 — 50 tests total for this module.

## Module 8 – Strategy Set Import (CSV/XLSX)

COMPLETED AND TESTED. An import *mechanism* only — an imported
Strategy Set is byte-for-byte the same kind of `StrategySet` object a
hand-built one is; there is no separate "Imported Strategy Set" model,
and nothing in `strategy_sets/`, `strategy_engine/`, `template_scanner/`,
or `database/` was modified to support it.

strategy_import/
    parsing.py       (parse_csv()/parse_workbook() -- SheetFrame; one
                     CSV file = one SheetFrame, one XLSX worksheet =
                     one SheetFrame, per the product rule "one sheet =
                     one Strategy Set")
    market_mapping.py  (resolve_market_code() -- three-way: supported /
                     unavailable / unrecognized, see below)
    validation.py         (validate_row() -- one row -> ReadyRow |
                     UnavailableRow | InvalidRow)
    preview.py               (build_preview() -- groups classified rows
                     per sheet into an ImportPreview; the identity-
                     deduplication and de-duplicated-naming logic live
                     here, see below)
    naming.py                  (unique_strategy_set_name() -- "Name",
                     "Name 2", "Name 3", ... generic name-collision
                     avoidance, reused for both Strategy Set names and,
                     within one set, colliding entry names)
    commit.py                    (commit_import() -- the ONLY function
                     in this package that writes to
                     StrategySetRepository)

ui/
    strategy_import_state.py, strategy_import_formatting.py,
    strategy_import_view.py  (upload -> preview -> Cancel/Import All;
                              see Module 6A's own "thin UI layer"
                              convention -- no analytics, filtering, or
                              persistence logic duplicated here)

tests/
    test_strategy_import_parsing.py, test_strategy_import_market_
    mapping.py, test_strategy_import_validation.py, test_strategy_
    import_preview.py, test_strategy_import_naming.py, test_strategy_
    import_commit.py, test_strategy_import_dedup.py, test_ui_strategy_
    import.py, test_ui_strategy_import_formatting.py

Key design points a future session needs:

- **Pipeline**: `parse_workbook()`/`parse_csv()` → `validate_row()` per
  row → `build_preview()` groups into an `ImportPreview` → nothing is
  written to `StrategySetRepository` until `commit_import()` is called
  explicitly, after the user reviews the preview. `build_preview()`
  only ever *reads* `StrategySetRepository.exists()` (to compute a
  de-duplicated name), never writes.
- **Vendor market codes vs. Oscill8's internal registry keys vs. LSEG
  RIC roots are three distinct things, deliberately kept separate**
  (see `strategy_import/market_mapping.py`'s own module docstring):
  a workbook's `Market` column uses short, RIC-root-*style* codes
  (`SRA`/`SON`/`CRA`/`ER`/`YBA`/`FSR`) that are the trader's own
  external vocabulary — these are NOT the same strings as `core.
  config.MARKETS`'s dict keys (`"SOFR"`, `"SONIA"`, `"CORRA"`, ...),
  and are not necessarily identical to the market's actual LSEG
  `MarketDefinition.ric_root` either (e.g. `ER`'s confirmed RIC root
  is `FEI`, `FSR`'s is `SARO3` — see below). `strategy_import.
  market_mapping` is the one place this translation happens; nothing
  else in the codebase interprets a workbook market-code string.
- **Three-way market-code resolution**, exactly mirroring `validation.
  py`'s `ReadyRow`/`UnavailableRow`/`InvalidRow` split:
  - **Supported** (`SUPPORTED_MARKET_CODES`): `SRA→SOFR`,
    `SON→SONIA`, `CRA→CORRA` — these three, and only these three,
    resolve to a real, configured `core.config.MARKETS` entry and can
    become a `ReadyRow`.
  - **Recognized but unavailable** (`UNAVAILABLE_MARKET_CODES`): `ER`
    (Euribor, confirmed RIC root `FEI`), `YBA` (an Australian exchange
    market), `FSR` (SARON 3M futures, confirmed RIC root `SARO3`).
    **None of these three has a `core.config.MarketDefinition` —
    `exchange`/`bp_per_point`/contract-rule metadata was deliberately
    never supplied or guessed, per the same "must not be invented"
    rule Module 1/Roadmap already established for EURIBOR.** A row for
    one of these markets is never silently dropped and never reported
    as an error — it becomes an `UnavailableRow` with its own specific,
    non-generic reason string, shown in the import preview, and is
    simply never persisted.
  - **Unrecognized** (anything else, e.g. a typo): an `InvalidRow`,
    shown with its row number, label, and reason — never silently
    dropped either.
- **Strategy identity for deduplication is the resulting
  `StrategyDefinition` (market + offsets + weights), never the
  human-facing Label** (`strategy_import/preview.py`'s
  `_dedupe_ready()`). A Label legitimately recurs across markets, and
  even within one market across genuinely different position
  structures — a real-workbook finding (the trader's own RBS Template
  workbook repeats labels like `"1Yr Fly"` across SRA/SON/CRA/ER rows
  by design). Two rows collapse to one entry iff their
  `StrategyDefinition`s are equal (reusing `StrategyDefinition`'s own
  dataclass equality directly); rows that survive dedup but still
  share a Label are disambiguated via `unique_strategy_set_name()`
  ("Name", "Name 2", ...), since `StrategySetEntry.name` uniqueness
  within a set is an existing, unmodified `strategy_sets/model.py`
  invariant this module must still satisfy.
- **Blank position cells and an explicit `0` are treated identically**
  — already true structurally before this module was even built:
  `template_scanner.templates.template_from_dense_weights()` (Module
  5A, unmodified) normalizes both to the same dense-weight value, so a
  blank-vs-zero difference alone never produces two distinct
  `StrategyDefinition`s.
- **Real-workbook regression: integer-typed Excel column headers**
  (`strategy_import/parsing.py::_frame_to_sheet()`). A trader-typed
  position-column header cell (`"1"`, `"2"`, ...) is very often stored
  by Excel as a *number*, not text — `pandas.read_excel` then reads
  `df.columns` as `int` for those columns, not `str`. Every actual row
  lookup (`series.get(...)`) MUST use the DataFrame's own,
  un-stringified column objects; only the *output* representation
  (`SheetFrame.position_columns`, each row dict's keys) is stringified,
  once, at the very end. Before this fix, every row in every worksheet
  of a real, integer-header-typed workbook was silently misclassified
  as blank and dropped before ever reaching `validate_row()` — CSV
  files were never affected (`pandas.read_csv` always parses header
  cells as text).
- **No interval is ever read from an uploaded file, and none is
  requested from the user at import time.** Every imported entry gets
  a fixed placeholder interval (`DEFAULT_IMPORT_INTERVAL = BarInterval.
  DAILY`) purely because `StrategyDefinition.interval` is a required
  field in the existing, unmodified schema (no `None`/optional state
  exists for it). This placeholder is inert for Module 9's Strategy
  Set Scan (which unconditionally overrides interval at run time
  regardless of what's stored); it only becomes user-visible/editable
  if an imported set's rows are loaded into the manual grid, where it
  behaves exactly like any freshly-typed row's Interval cell.
- **Duplicate Strategy Set names are never overwritten.**
  `unique_strategy_set_name()` produces `"Name"`, then `"Name 2"`,
  `"Name 3"`, ... — re-importing the same workbook creates additional,
  distinctly-named sets rather than silently replacing the earlier
  import.
- **Invalid worksheet/file names are never auto-sanitized.** If a
  sheet or file name fails `StrategySet`'s own name pattern (letters,
  digits, spaces, `-`, `_` only — e.g. a name containing `&`), that
  sheet is reported as a blocking `sheet_error` telling the user to
  rename it at the source; it is never silently renamed.
- Test suite (current file-level counts): `test_strategy_import_
  parsing.py` 18, `test_strategy_import_market_mapping.py` 13, `test_
  strategy_import_validation.py` 18, `test_strategy_import_preview.py`
  15, `test_strategy_import_naming.py` 5, `test_strategy_import_
  commit.py` 7, `test_strategy_import_dedup.py` 11, `test_ui_strategy_
  import.py` 8, `test_ui_strategy_import_formatting.py` 14 — 109 tests
  total for this module.

**Deferred, not solved here**: a full `MarketDefinition` (and any real
LSEG/QH data-provider support) for EURIBOR/YBA/SARON — see the
Development Roadmap's Deferred list; the "ask the user for a RIC at
import time" mechanism mentioned during design is a separate,
not-yet-approved future feature, not implemented.

## Module 9 – Strategy Set Scan (Run-Time Execution)

COMPLETED AND TESTED. A second, additive way to run a saved Strategy
Set — separate from, and without changing, Module 7B's grid-based Run
Scan (per-row Market/Interval, `run_scan()`, all unchanged).

strategy_sets/
    execution.py   (with_interval_override(), run_strategy_set())

ui/
    strategy_set_scan_view.py   (interval selectbox + "Run" button,
                                shown only when a saved set is
                                selected; stores its result via the
                                SAME ui.state.store_scan_result() the
                                grid path already uses)

tests/
    test_strategy_sets_execution.py

Key design points a future session needs:

- **The desired workflow**: select a saved Strategy Set → pick ONE
  interval → that interval applies to every entry in the set for that
  run only. The user should not have to edit every entry's interval
  individually, and the same saved set should be runnable at different
  intervals on different occasions without modification or re-import.
- **The interval override is applied to a transient, in-memory copy of
  the `StrategySet`, never to the persisted object.**
  `with_interval_override(strategy_set, interval)` uses
  `dataclasses.replace()` (an established pattern already used by
  `strategy_sets/repository.py`'s `rename()`/`duplicate()`) to build a
  new `StrategySet` whose every entry's `StrategyDefinition.interval`
  is replaced — the saved file, and whatever is on disk under that
  set's name, is completely untouched by running a scan. **This is the
  same, deliberate "call-time argument, never persisted state"
  precedent Module 7A already established for `contract_start`/
  `contract_end`.**
- **The override happens BEFORE `expand_strategy_set()`, not after.**
  `expand_strategy_set()` calls `template_scanner.universe.
  dedupe_candidates()` internally, which keys deduplication on
  `(market_key, rics, weights, interval, price_field)`. Overriding
  interval only after expansion/dedup would risk two originally-
  different-interval entries becoming interval-identical post-override
  without ever having been deduplicated against each other — a real
  double-counting risk. Overriding first means dedup runs on the
  already-uniform interval and dedupes correctly.
- **Zero changes to `strategy_sets/expansion.py` or `template_scanner/
  scanner.py`.** `run_strategy_set()` composes the completely
  unmodified `expand_strategy_set()` → `run_scan_on_instances()` —
  `run_scan_on_instances()` is not a parallel implementation of
  `run_scan()`; `run_scan()` itself calls it internally after its own
  candidate-generation step, so this path reuses the exact same
  pricing, `MarketDataUnavailableError` skip-handling, and Module
  4A/4B analytics, verbatim, with no risk of behavioral drift between
  the grid path and this one.
- **`ScanRequest` compatibility**: `run_scan_on_instances()` returns
  only a `ScanReport`, but `ui/chart_view.py` reads `scan_request.
  price_start`/`.price_end`/`.lookbacks` directly out of session state.
  `run_strategy_set()` therefore also builds and returns a `ScanRequest`
  (from the overridden entries' definitions and the same call-time
  window/lookback/percentile arguments) purely so the existing results/
  chart UI keeps working completely unmodified.
- Test suite (current file-level count): `test_strategy_sets_
  execution.py` 13 tests.

## CORRA Entitlement Error Classification (LSEG 70112)

COMPLETED AND TESTED. A targeted fix to `core/downloader.py`'s error
classification, not a scanner or UI change.

Module 1's original findings already documented that CORRA's current
LSEG account lacks entitlement for its universe (`TS.Interday.
UserNotPermission.70112`, "User does not have permission for this
universe" — see Module 1 above). That condition was not previously
recognized by `core.downloader`'s classification, so it propagated as
a raw, uncaught exception through `run_scan_on_instances()` — one
CORRA leg's entitlement error aborted an ENTIRE mixed-market scan,
discarding otherwise-successful results for every other market in it,
since the scanner's existing skip-and-continue machinery (Module
5B.1) only recognized the narrower `70005` "universe is not found"
condition.

core/downloader.py:
    _is_confirmed_no_permission()   (sibling to the existing
                                    _is_confirmed_universe_not_found(),
                                    same exact-match philosophy)

Key design points a future session needs:

- **Same narrow, exact-match philosophy as the existing `70005`
  classification** — LSEG's `LDError` type + the exact confirmed error
  code `TS.Interday.UserNotPermission.70112` + the exact confirmed
  phrase `"User does not have permission for this universe"` (quoted
  verbatim from Module 1's live-tested evidence, never guessed). Both
  conditions translate to the SAME, existing `MarketDataUnavailableError`
  — `run_scan_on_instances()` needed zero changes of its own; its
  existing per-candidate skip-and-continue machinery already handles
  either condition correctly once the right exception type is raised.
- **CORRA's entitlement gap is interval-independent** — `70112` is a
  permissions issue, not a data-availability-at-a-given-granularity
  issue, so CORRA is skipped identically at `DAILY`/`HOURLY`/`4H`.
- **The scanner's deliberately narrow exception policy is unchanged
  and was NOT broadened** to accommodate this — an unrelated exception
  (a real network/session/auth/vendor error, a programming bug) still
  propagates and aborts the whole scan, exactly as before. The fix
  lives entirely in `core.downloader`'s classification layer, which
  already exists specifically to translate LSEG's vendor errors into
  typed conditions — extending it with one more confirmed code is
  reusing its existing responsibility, not creating a new one.
- **SONIA's `HOURLY`/`4H` data availability is a SEPARATE, still-open
  question — NOT resolved by this fix, and not resolved at all.** No
  repository evidence (live-tested or otherwise) exists for SONIA's
  exact LSEG error code at intraday intervals; nothing should be
  implemented for it until that evidence exists (see the Development
  Roadmap's Deferred list). SONIA's `DAILY` behavior (Module 1,
  confirmed working live) is unaffected either way.
- Test suite additions: `tests/test_downloader.py` (+10:
  `_is_confirmed_no_permission()` unit coverage, a `download_history()`
  end-to-end translation test), `tests/test_template_scanner_
  scanner.py` (+2: a mixed SOFR+CORRA scan proving the skip-and-
  continue behavior, and proving an unrelated exception on the CORRA
  leg still aborts the whole scan).

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

# Architectural Decisions

Established, standing decisions — not proposals, and not specific to
any one module. Cite the section above for the module where each is
actually enforced in code.

1. **Never guess market metadata.** A `core.config.MarketDefinition`'s
   `exchange`/`bp_per_point`/contract-rule fields are mandatory with no
   default and must never be invented — a wrong `bp_per_point` in
   particular would silently corrupt every bp-denominated
   `range_analytics` metric for that market. This is why EURIBOR/YBA/
   SARON have no `MarketDefinition` today even though their RIC roots
   are known (see Module 8 above and the Deferred list below) — a
   known RIC root is not sufficient to add a market; the full metadata
   set is required.
2. **Vendor-facing market codes are kept structurally separate from
   Oscill8's internal `core.config.MARKETS` registry keys.** A
   workbook's short market code (`SRA`/`SON`/`CRA`/`ER`/`YBA`/`FSR`)
   is the trader's own external vocabulary; `core.config.MARKETS`'s
   dict keys (`"SOFR"`, `"SONIA"`, ...) are Oscill8's internal
   registry keys; a market's actual LSEG RIC root
   (`MarketDefinition.ric_root`) is a third, independent thing that
   need not match either. `strategy_import.market_mapping` is the one
   explicit bridge between the first and second; nothing else in the
   codebase interprets a vendor market-code string. See Module 8 above.
3. **A Strategy Set's run-time interval is a call-time argument, never
   persisted Strategy Set state.** Established first for
   `contract_start`/`contract_end` (Module 7A — baking an absolute
   date range into a saved, reused object goes stale the moment "today"
   moves past it) and applied identically to interval (Module 9 — the
   same saved set must be runnable at different intervals on different
   occasions without modification or re-import). A Strategy Set
   describes *what* to scan; *when*/*at what granularity* it's scanned
   is supplied at expand/run time.
4. **Data-availability handling belongs at the data-provider layer
   (`core.downloader`), not as product-specific exceptions scattered
   through the scanner.** `MarketDataUnavailableError` and its narrow,
   exact-match classification predicates (`_is_confirmed_universe_not_
   found`, `_is_confirmed_no_permission`) are the single place LSEG's
   vendor errors are translated into a typed, skippable condition —
   `run_scan_on_instances()`'s skip-and-continue machinery (Module
   5B.1) needed zero changes to correctly handle CORRA's `70112` once
   it was classified there (see the CORRA section above). A future
   market-specific failure mode should be handled the same way — one
   more narrow predicate in `core.downloader`, not a broadened
   `except Exception` anywhere in `template_scanner`.

---

# Development Roadmap

Module 1 — LSEG data layer — STATUS: COMPLETE
Module 2 — SQLite market-data cache — STATUS: COMPLETE
Module 3 — Strategy engine — STATUS: COMPLETE
Module 4A — Range-bound analytics — STATUS: COMPLETE
Module 4B — Multi-lookback / stability analytics — STATUS: COMPLETE
Module 5A — Template / candidate universe engine — STATUS: COMPLETE
Module 5B — Scanner orchestration, filtering, ranking (incl. 5B.1
unavailable-market-data hardening and the canonical metric-resolution
fix) — STATUS: COMPLETE
Module 6A — Streamlit range-bound scanner UI — STATUS: COMPLETE
Module 6B — Selected-strategy history chart — STATUS: COMPLETE
Module 7A — Strategy Set engine (domain model, JSON persistence,
expansion to StrategyInstance[]) — STATUS: COMPLETE
Module 7B — Strategy Set UI (grid unification: selector, Save/+ New/
Delete, per-row Market/Interval) — STATUS: COMPLETE
Module 8 — Strategy Set import (CSV/XLSX, preview, ready/unavailable/
invalid classification, StrategyDefinition-identity dedup) —
STATUS: COMPLETE
Module 9 — Strategy Set Scan (run-time, transient interval override) —
STATUS: COMPLETE
CORRA entitlement error classification (LSEG 70112 ->
MarketDataUnavailableError) — STATUS: COMPLETE

Current suite: 879 tests passing, 1 skipped (`pytest -q`; re-run for
the up-to-date count, do not trust this number blindly — see README.md's
Testing section). Verified directly (`-rs`): the 1 skip is `tests/
test_ui_keyboard_browser.py`, skipped when Playwright/Chromium isn't
available in the current environment (it wasn't in the environment
this count was verified in) — a real-browser keyboard-workflow check,
unrelated to LSEG. `test_live_connection.py` is a standalone script
with no `test_*` functions at all (confirmed via `pytest --collect-
only`) — it is never collected by `pytest -q` regardless of
environment; it must be run directly (`python test_live_connection.py`)
against a live LSEG Workspace session.

Deferred / not yet implemented (do not assume any of these exist merely
because they're listed here as being considered):

- Configurable robust-range percentile bounds (today's 5th/95th
  percentile bounds in `range_analytics/location.py` are hard-coded).
- Z-score / current-dislocation analytics distinct from the existing
  `RangeAnalytics.z_score` field — exact statistical definition not
  approved.
- Intermarket strategies (legs spanning more than one market).
- An explicit "Real Contract" scanning mode (pick one specific set of
  dated contracts rather than a rolled template) — the backend
  primitives it would need already exist (`StrategyInstance`,
  `build_history()`, `analyze_histories()`,
  `core.futures_calendar.generate_contracts()`), but `run_scan()` has no
  instances-in/`ScanReport`-out entry point with skip-handling exposed
  for a UI to call without duplicating `scanner.py`'s internal loop.
- A true Generic-vs-Real-contract mode distinction — today's scanner is
  a single position-relative rolling-template mode; it is not a
  continuous, contract-independent "generic curve" series.
- A composite Range Score (filtering/ranking stay transparent,
  single-metric, never a blended/opaque score).
- Secondary diagnostic charts beyond Module 6B's one primary
  strategy-history chart (AR(1) fit, crossing markers, rolling
  stability, z-score).
- Saved scans / export workflow (distinct from Module 7A's Strategy
  Sets — a Strategy Set is a named collection of strategy definitions
  only; it does not capture a price window, lookbacks, or results, and
  Module 9's Strategy Set Scan interval selection is a call-time-only
  choice, never saved alongside the set).
- Cloud/server deployment and any non-desktop LSEG authentication.
- **Full `MarketDefinition` configuration for EURIBOR, YBA, and SARON
  (FSR)** — `strategy_import.market_mapping` already recognizes all
  three markets by name (`ER`/`YBA`/`FSR`, see Module 8 above) and
  their RIC roots are confirmed (`EURIBOR`: `root="FEI"`; `YBA`: an
  Australian exchange market, `root="YBA"`; `SARON` 3M futures:
  `root="SARO3"`), but none has a `core.config.MarketDefinition` — the
  mandatory `exchange`/`bp_per_point` fields (no default) have not been
  supplied for any of the three and must not be invented (see
  Architectural Decision 1 above). **None of these three markets is
  currently tradable/scannable through LSEG or any other data
  provider** — they exist today only as a recognized-but-unavailable
  classification in the importer. Add each once its metadata is
  supplied; no other change needed for that market's RIC handling —
  `core.ric`/`core.futures_calendar` already support it generically.
- **SONIA's `HOURLY`/`4H` data availability is unverified** — SONIA's
  `DAILY` behavior is confirmed working live (Module 1), but no
  repository evidence exists for what LSEG returns for SONIA at
  intraday intervals, and nothing has been implemented for it (see the
  CORRA Entitlement Error Classification section above). Requires a
  live LSEG call to capture the actual error/response before any
  classification or handling is added — do not guess or assume it
  behaves like CORRA's `70112` case.
- **QuantHub (QH) as a secondary/fallback data provider** — mentioned
  as a future direction for markets like CORRA whose LSEG entitlement
  is currently missing, but **no QH code, integration, or design exists
  anywhere in this repository today.** Not started.
- An "ask the user for a RIC at import time" mechanism for a
  recognized-but-unavailable imported market — mentioned during Module
  8's design as a possible future feature, **not implemented, not
  designed, not approved.**
- One-digit-year RIC collision across decades for 1-digit-year markets
  (SONIA/CORRA/€STR) — see `core/config.py`'s `MarketDefinition.
  ric_year_digits` docstring. Documented, not solved; not a live issue
  at today's practical STIR contract-generation horizon.

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