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

Current suite: 399 tests passing (`pytest -q`; re-run for the
up-to-date count, do not trust this number blindly — see README.md's
Testing section).

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
- Saved scans / export workflow.
- Cloud/server deployment and any non-desktop LSEG authentication.
- EURIBOR market (`root="FEI"`, `ric_year_digits=1`, QUARTERLY) — RIC
  convention confirmed by the trader, but `MarketDefinition.exchange`
  and `MarketDefinition.bp_per_point` (both mandatory, no default) have
  not been supplied and must not be invented (`bp_per_point` in
  particular drives `range_analytics`' bp conversions — a wrong/guessed
  value would silently corrupt every bp-denominated metric for this
  market). Add once that metadata is supplied; no other change needed —
  `core.ric`/`core.futures_calendar` already support it generically.
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