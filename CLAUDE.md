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
    not alter CORRA's root/year-digit convention or add a fallback
    identifier to work around this at the RIC layer. **Superseded/
    extended by the QuantHub provider layer** (see the "QuantHub
    Secondary Provider" module below): CORRA is one of six markets
    routed to QuantHub as a fallback ABOVE this RIC layer when LSEG
    cannot serve it -- the RIC convention itself is untouched by that
    fallback and remains exactly as documented here.
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
  the current HOURLY bar) is never cached or marked synced. **Superseded
  by the "Effective Request End" fix documented in the QuantHub/
  Provider-Provenance module below**: as originally built here, such a
  bar was still returned to the caller for that one call (never cached,
  but visible in the result) and re-fetched from LSEG every time until
  it closed. That "still returned" behavior no longer holds — a
  currently-forming bar for ANY interval is now excluded from the
  request entirely and never appears in the returned DataFrame until it
  actually closes, which also fixed a real repeated-request problem the
  original design didn't anticipate. See that section for the current,
  accurate behavior; this bullet is kept only for historical context on
  why a bar is never cached mid-formation in the first place.
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

- **Robust range bounds are CONFIGURABLE, not hard-coded.**
  `location.py`'s `range_low_robust(series, lower_percentile=5.0)`/
  `range_high_robust(series, upper_percentile=95.0)`/
  `range_width_robust(series, lower_percentile=5.0, upper_percentile=95.0)`
  each take the percentile bound(s) as parameters (default 5.0/95.0,
  preserving the original behavior when unspecified);
  `location.validate_percentiles(lower, upper)` enforces
  `0 <= lower_percentile < upper_percentile <= 100`.
  `RangeAnalytics`/`analyze_range()` accept and store the same
  `lower_percentile`/`upper_percentile` (also threaded through
  `analyze_multi_lookback()` and `ScanRequest`, all the way to the UI's
  own Lower/Upper %ile inputs — see Module 6A). This superseded an
  earlier hard-coded-P5/P95 design; the note that a "configurable
  percentile band" was merely under consideration is stale and no
  longer applies.
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
- Same-market templates only — this module's own dense-grid template
  generator (`template_from_dense_weights()`/`generate_candidates()`)
  has no intermarket (cross-market-leg) equivalent and was not modified
  to add one. Cross-market legs within a single strategy are instead
  handled by the separate, additive `strategy_engine.intermarket_*`
  domain model + `strategy_sets.IntermarketStrategySetEntry` (see
  Module 9) — a materially different combinatorial problem, addressed
  as a sibling module rather than a modification of this one, exactly
  as anticipated here.
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

COMPLETED AND TESTED. Shipped across several passes — an initial
minimal functional scanner, a compact trader-facing redesign, a full
dark "trading terminal" UI/UX redesign, the Module 7B Strategy Set
panel and Strategy Set Import integration (documented separately
above), and most recently the Range-Bound Opportunities Market
filter/column selector/Strategy Label enhancement. This section
describes the CURRENT state of the core scanner UI as the code reads
today, not each historical pass individually.

ui/
    __init__.py     (package docstring / file-responsibility map)
    app.py           (entry point -- `streamlit run ui/app.py`; loads
                     `.env` via `python-dotenv` before any Oscill8
                     import, see the Environment Configuration note
                     under Module 8)
    state.py          (session-state keys: expensive scan result +
                     selected-candidate/history cache -- see Module 6B)
    controls.py        (Strategy Workspace -- Strategy Set selector +
                     ONE strategy grid, rendered ABOVE Scan
                     Configuration -- Interval/Contracts/History/
                     Analytics/Run Scan)
    scan_view.py         (Run Scan: builds ScanRequest + a label-by-
                     definition-id map, calls run_scan() exactly once
                     per press, UI-boundary error handling)
    results_view.py        ("Range-Bound Opportunities": status, Market
                     filter, ranking/filters/columns popovers, ranked
                     result grid, row -> ScanCandidateResult selection,
                     Selected Strategy summary panel, skipped-candidates
                     expander)
    formatting.py            (pure helpers: strategy-grid-row ->
                     StrategyDefinition translation, filter/sort-key
                     construction from template_scanner's own accessor
                     factories, ranked-by/rank-column/column-selection/
                     market-list formatting -- zero Streamlit import,
                     fully unit-testable)

See Module 7B and Strategy Set Import above for `ui/strategy_set_*.py`/
`ui/strategy_import_*.py`, and `ui/error_formatting.py` for the pure
exception -> trader-facing-headline translation `ui.scan_view` uses
(never a traceback/vendor error code as the primary error message).

tests/
    test_ui_formatting.py (65), test_ui_controls.py (17) -- 82 tests
    for this section specifically; re-run `pytest -q tests/test_ui_
    formatting.py tests/test_ui_controls.py` for the up-to-date count
    rather than trusting a number recorded here.

Key design points a future session needs:

- **The UI is a thin layer, structurally.** It never computes analytics,
  never duplicates filtering/ranking/derived-metric formulas, never
  imports `lseg.data`. `formatting.py` has zero Streamlit dependency by
  design — every filter/sort accessor it builds is a closure over
  `template_scanner.filters.at_lookback()`/`stability()`, never a
  reimplementation.
- **Render order: Strategy Workspace above Scan Configuration**
  ("what am I scanning?" before "how should it be measured?") —
  reversed from the module's original order. `_render_strategy_
  templates()` (`ui/controls.py`) no longer needs a completed Scan
  Configuration Interval selection to render first: it peeks the scan
  bar's own Interval widget key as it stood after the PREVIOUS rerun
  (`_peek_current_interval()`) purely to seed a brand-new blank row's
  default Interval cell — cosmetic only, never authoritative; every
  already-populated row keeps carrying its own persisted Interval
  regardless of render order.
- **No global Market selector; the grid's own per-row Market/Interval
  are authoritative for WHAT gets priced.** Scan Configuration's ONE
  Interval selector is a RUNTIME-ONLY override applied to every leg at
  scan time (`ui.formatting.apply_interval_override()`, applied by
  `ui.scan_view.handle_run_scan()`) — a row's own persisted Interval
  cell (still shown/edited, still what a Strategy Set saves) can never
  silently conflict with what a scan actually executes at. There is no
  equivalent Market override: each row's own Market always determines
  which market that leg prices against.
- **Universe/Contracts is fully automatic, not a user-entered date
  range.** `_default_universe_window(today)` (`ui/controls.py`) always
  returns `(today, today + 730 days)` — Oscill8 scans the CURRENTLY
  active contract curve, shown as a read-only "📈 Automatic" indicator,
  never editable date inputs. "Today" is exactly the boundary
  `core.futures_calendar.generate_contracts()` (the same function every
  rolling scan already calls) uses to decide which contract-months are
  still eligible, so no separate expiry calendar is needed to exclude
  already-elapsed months. **Price History** (`price_start`/`price_end`
  — what date range gets priced/analyzed) remains a completely separate,
  still user-editable concept, defaulting to the last ~6 months
  (`_default_history_window()`).
- **The robust-range percentile band is a live UI control** — "Lower
  %ile"/"Upper %ile" number inputs (default 5/95, `ui/controls.py`),
  flowing into `ScanRequest.lower_percentile`/`upper_percentile` and
  from there into every `range_analytics` call unchanged (see the
  corrected Module 4A note above) — this is NOT hard-coded.
- **Pipeline order matters**: filter (metric filters from the Filters
  popover, AND the Market multiselect) and rank the Python
  `ScanCandidateResult` objects FIRST (`apply_filters` -> market-key
  filter -> `rank_results`), THEN build the display DataFrame
  (`results_to_dataframe` -> `to_display_dataframe` -> `add_rank_column`
  -> `apply_column_selection`) — row position in the displayed grid
  must map back to the same-position entry in the ranked candidate
  list, since row-selection in `st.dataframe` only gives back a
  positional index. Column selection is the LAST step, purely display-
  layer, after Rank is already assigned — hiding/showing a column never
  affects Rank or row selection.
- **Market filter** (`ui.results_view._render_market_filter()`,
  `ui.formatting.available_markets()`): a `st.multiselect` whose options
  are the distinct `market_key` values in the CURRENT scan's own
  `report.results` — recomputed fresh every render, never a fixed/
  global market list. Defaults to every market selected ("All
  Markets"); the persisted selection is reset back to "all" only when
  it would otherwise reference a market no longer present (Streamlit
  raises if a multiselect's stored value isn't a subset of its current
  options) — a selection that's still valid for a new scan's market set
  is left alone, matching how Ranking/Filters already persist across
  scans. Filtering here only changes which ROWS are displayed; it never
  touches `strategy_engine`/`database`/provider/cache logic, and `Rank`
  (assigned after this filter runs) stays contiguous over exactly
  what's shown.
- **Column selector ("Columns ▾" popover)**: `ui.formatting.
  OPTIONAL_COLUMN_LABELS` is `RANK_COLUMN` plus every `DISPLAY_COLUMNS`
  label, in table order — EVERY column, including `Rank` and
  `Strategy`, is independently optional (nothing in row-selection
  depends on any particular column being visible, since `st.dataframe`
  selection is positional). `DEFAULT_VISIBLE_COLUMNS` is every label
  except `"Strategy Label"` (see below) — i.e. the original 14 columns
  stay visible by default, preserving the table's pre-existing
  appearance exactly. `apply_column_selection(display_df,
  selected_labels)` is a pure display-layer projection — it does NOT
  force-keep any column — that preserves `display_df`'s existing column
  order rather than the order columns were selected in (so toggling one
  column never reshuffles the rest), and never removes the underlying
  metric from `ScanCandidateResult`/`results_to_dataframe()`. Computed
  fresh every rerun from the `"oscill8_visible_columns"` `st.multiselect`
  widget state, with no separate persistence layer.
- **Strategy Label column** (`STRATEGY_LABEL_COLUMN`, sourced from the
  new `ScanCandidateResult.label` field): shows the ACTUAL originating
  Strategy Set entry name / strategy-grid row Label — never re-derived
  or reconstructed from RICs or weights. `ui.scan_view.handle_run_scan()`
  builds a `{id(definition): row.label}` map right where grid rows are
  translated into `StrategyDefinition`s, and passes it to `run_scan(
  request, labels_by_definition_id=...)`; `template_scanner.scanner.
  run_scan_on_instances()`/`analyze_histories()` thread it through by
  `id(instance.definition)` (safe because one grid row's
  `StrategyDefinition` object is reused BY REFERENCE across every
  rolled candidate it produces — `strategy_engine.combinations.
  generate_instances()`/`template_scanner.universe` never clone or
  replace it) onto each surviving `ScanCandidateResult.label`. `None`
  when no mapping is supplied (e.g. a direct `run_scan_on_instances()`/
  `analyze_histories()` call, or a manually-typed row with a blank
  Label — which itself gets an auto-generated `"Strategy {i+1}"` label
  at grid-translation time, per `build_definitions_from_grid()`'s
  existing convention). Available in the Columns popover; NOT in
  `DEFAULT_VISIBLE_COLUMNS`, since it's a genuinely new column and the
  spec calls for the table's default appearance to stay unchanged.
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
- **`Ranking ▾`/`Filters ▾`/`Columns ▾` use `st.popover`**, not
  `st.expander` — floating panels that don't push the results table
  down. The "Ranked by: ..." label is built by reading the Ranking
  popover's persisted `st.session_state` values BEFORE that popover's
  own widgets are (re)created later in the same script pass
  (`_current_rank_state()` in `results_view.py`) — safe because nothing
  changes state between that read and the widgets' own creation in the
  same rerun.
- No new backend capability was needed for the Market filter/column
  selector/Strategy Label work — the market filter reads
  `ScanCandidateResult.market_key`, which already existed (populated via
  `resolve_display_market_key()`, see Module 9); only `label` is a
  genuinely new field, added because no existing structure carried a
  caller-facing name through to the result at all.

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

**Deferred, not solved here** (see the Development Roadmap below):
Streamlit UI, a strategy editor, wiring `StrategySet`/`expand_strategy_
set()` output into a running scan, watchlists, alerts, deployment. Most
of this list is **superseded by Module 7B** below, which shipped a
Streamlit UI (a Strategy Set selector integrated directly into the
existing scanner grid) — but via a DIFFERENT, simplified mechanism than
originally anticipated here: it never calls `expand_strategy_set()`/
`StrategySetEntry` per-entry expansion at all (see Module 7B's own
design-principle note). That richer per-entry execution path
(`expand_strategy_set()` → `template_scanner.scanner.
run_scan_on_instances()`, plus `strategy_sets/execution.py`) still
exists and is still tested, but remains genuinely unreachable from any
live UI button today. True intermarket (cross-market-leg) strategies
were deferred at the time this module was written but have SINCE been
implemented as an additive sibling — see Module 9 below
(`strategy_engine.intermarket_*` + `strategy_sets.
IntermarketStrategySetEntry`/`intermarket_entries`); an intermarket
entry can ONLY be authored by hand-editing a Strategy Set's JSON file
today — Module 7B's grid-based UI has no representation for it (see
Module 7B's own note) — and, like the richer per-entry path in general,
nothing in the live UI triggers a scan of one. Watchlists, alerts, and
deployment remain unaddressed.

---

## Module 7B – Strategy Set UI Integration (Simplified)

COMPLETED AND TESTED. The Streamlit UI's own name for this work — see
`ui/__init__.py`, `ui/strategy_set_view.py`, `ui/strategy_set_state.py`,
and `ui/strategy_set_formatting.py`'s module docstrings, all of which
self-identify as "Module 7B" (a later simplification of an even
earlier separate-panel version of this same module, which is not
described here — only the current, simplified design is).

Design principle: **"Strategy Templates is the working strategy grid; a
Strategy Set is simply a saved named version of that grid."** There is
no second grid, no separate "Run '<Strategy Set>'" button, and no
Strategy-Set-specific pricing/execution path — a loaded Strategy Set
becomes ordinary grid rows, and `ui.scan_view.handle_run_scan()` builds
`StrategyDefinition`s from those rows via the exact same
`ui.formatting.build_definitions_from_grid()` a manually-typed row goes
through, with no idea (and no need to know) whether a given row was
typed or loaded from a saved set. This is a deliberate simplification
FROM an earlier, richer per-`StrategySetEntry` execution design (which
still exists in `strategy_sets/expansion.py`/`execution.py` and
`template_scanner.scanner.run_scan_on_instances()`, all still tested,
but is not what this UI module calls — see Module 7A's closing note
above).

ui/
    strategy_set_view.py         (selector + Save/"+ New"/Delete/render_
                                 selector(), integrated into ui.controls'
                                 Strategy Templates section header —
                                 not a separate page section)
    strategy_set_state.py          (session-state: which saved set (if
                                 any) is currently loaded, the pending-
                                 selection indirection a Save action
                                 needs — see its own widget-lifecycle
                                 note below — and a one-shot status
                                 message; no separate "draft" state,
                                 since the grid widget itself IS the
                                 draft)
    strategy_set_formatting.py       (pure translation between grid rows
                                 <-> StrategySet/StrategySetEntry — the
                                 layer that makes a per-row Market/
                                 Interval round-trip losslessly)

tests/
    test_ui_strategy_set_formatting.py, test_ui_strategy_set_state.py,
    test_ui_strategy_set_selector_lifecycle.py,
    test_ui_strategy_set_multimarket_roundtrip.py

Key design points a future session needs:

- **Per-row Market/Interval, not a global selector.** The grid's own
  `Label`/`Market`/`Interval` `SelectboxColumn`s (see `ui.controls`) are
  what let a Strategy Set mix markets/intervals across its rows (e.g. a
  SOFR + SONIA + CORRA set) and round-trip through load → edit → save →
  reload without any row's market silently changing. `ui.controls` has
  **no global Market selector at all** (removed as part of this
  simplification) — a Strategy Set's markets are exactly whatever its
  rows carry. The scan bar's one remaining **Interval** selector is a
  RUNTIME-ONLY override (`ui.formatting.apply_interval_override()`,
  applied by `handle_run_scan()`): every row's own persisted Interval
  cell still round-trips through save/load unchanged, but every leg of
  an actual scan runs at whatever the scan bar's Interval selector
  currently shows.
- **Widget-lifecycle fix**: Streamlit forbids writing to a widget's own
  session-state key once that widget has already been instantiated in
  the current script run. A Save/rename/duplicate/delete action runs
  further down the same script pass than the selector widget, so it
  cannot write to the selector's key directly — it sets `strategy_set_
  state.set_pending_selection(name)` and calls `st.rerun()`; on the
  FRESH rerun that follows, `render_selector()` applies that pending
  value to the widget's key before the widget is (re)created — the one
  point where doing so is legal.
- **No intermarket EDITING; read-only visibility only (TASK-001).**
  The grid itself composes ordinary, single-market `StrategySetEntry`/
  `StrategyDefinition` rows only — a `StrategySet.intermarket_entries`
  (Module 9) is never shown or editable *in the grid*. It is no longer
  invisible, though: `ui.strategy_set_view.render_intermarket_entries()`
  renders a READ-ONLY `st.dataframe` panel below the grid (entry name,
  enabled flag, interval, price field, optional `bp_per_point`, one row
  per `LegSpec` with that leg's own market/offset/weight), rendered only
  when the loaded set actually has such entries — a single-market set's
  appearance is unchanged. Translation lives in the new, Streamlit-free
  `ui/intermarket_formatting.py`; its composite market label is Module
  9's `resolve_display_market_key()` and stays display-only (never
  provider resolution, cache lookup, or bp conversion). Nothing in `ui/`
  can create/edit/delete one — hand-editing the JSON is still the only
  authoring route. Saving preserves them verbatim: `build_strategy_set_
  from_grid()` takes an `intermarket_entries` parameter (supplied by
  `process_save()`/`_save()` from the set loaded earlier in the same
  script pass) and writes them back unchanged through the same single
  `repo.save()` — previously a save silently dropped them. An
  intermarket-only set (blank grid) is saveable; a wholly empty set is
  still rejected.
- Test suite (current file-level counts): see the files listed above;
  run `pytest -q tests/test_ui_strategy_set_formatting.py tests/
  test_ui_strategy_set_state.py tests/test_ui_strategy_set_selector_
  lifecycle.py tests/test_ui_strategy_set_multimarket_roundtrip.py` for
  the up-to-date count rather than trusting a number recorded here.

---

## Strategy Set Import (Excel/CSV)

COMPLETED AND TESTED. An additive IMPORT MECHANISM on top of the
existing, unmodified `strategy_sets`/Module 7B UI — not a separate
persisted model. Turns an uploaded Excel workbook or CSV file into
ordinary `strategy_sets.StrategySet` objects; an imported set is
byte-for-byte the same kind of object a hand-built one is and is
immediately visible to/usable by the Module 7B selector.

strategy_import/
    __init__.py       (public re-exports; the pipeline overview lives
                     here, mirrored below)
    parsing.py          (parse_workbook()/parse_csv() -> list[SheetFrame]
                     -- pure, no market-code resolution)
    validation.py         (validate_row() -> ReadyRow | UnavailableRow |
                     InvalidRow -- reuses template_scanner.templates.
                     template_from_dense_weights() directly, never via
                     ui.formatting, to avoid an inverted ui->domain
                     dependency)
    market_mapping.py       (resolve_market_code() -- trader-facing RIC-
                     root-style codes, e.g. "SRA"/"SON"/"CRA", to
                     core.config.MARKETS registry keys, e.g. "SOFR"/
                     "SONIA"/"CORRA"; three-way: supported/unavailable/
                     unrecognized)
    naming.py                 (unique_strategy_set_name() -- "Name 2",
                     "Name 3", ... suffixing, never "Name (2)": "(" is
                     not in StrategySet's own name pattern)
    preview.py                  (build_preview() -> ImportPreview; the
                     ONE read against StrategySetRepository, only to
                     compute a de-duplicated name -- never a write)
    commit.py                     (commit_import() -- the ONLY write
                     boundary, called only after explicit user
                     confirmation of the preview)

ui/
    strategy_import_view.py    (upload -> preview -> "Import All" panel,
                              rendered from an Import button inside the
                              Strategy Templates section header)
    strategy_import_state.py     (session-state: whether the panel is
                              open, the current in-memory ImportPreview,
                              which file it was built from, the one-shot
                              post-import summary)

tests/
    test_strategy_import_parsing.py, test_strategy_import_validation.py,
    test_strategy_import_market_mapping.py, test_strategy_import_naming.py,
    test_strategy_import_preview.py, test_strategy_import_commit.py,
    test_strategy_import_dedup.py, test_ui_strategy_import.py,
    test_ui_strategy_import_formatting.py

Key design points a future session needs:

- **One worksheet (Excel) or one file (CSV) is exactly one Strategy
  Set.** Expected column shape: a `Market` column, a `Label` column
  (both matched case-insensitively), and one or more curve-position
  columns holding a leg's weight (0/blank = "no leg at this position") —
  the same dense-weight convention the manual grid uses. No interval
  column is expected or read; every imported entry gets a fixed
  `DEFAULT_IMPORT_INTERVAL` placeholder (the interval a trader actually
  cares about is chosen at RUN time via Scan Configuration's Interval
  selector, applied to every leg regardless of what's stored).
- **Three-way row classification, never a two-way valid/invalid split.**
  `ReadyRow` / `UnavailableRow` (a market Oscill8 recognizes by name but
  has no configured `MarketDefinition` for — currently none, since
  EURIBOR/SARON/YBA/ESTR_ICE all have entries today) / `InvalidRow` (bad
  market code, or a position cell that's non-blank and not a valid
  number — deliberately STRICTER than the manual grid's own `_cell_to_
  float`, which silently treats an unparseable live-edit cell as 0; an
  imported file has no "mid-edit" state, so an unparseable cell is a
  genuine data problem, reported, never silently coerced to 0). A row
  where every position column is blank/NaN is dropped silently — not an
  error, matching the manual grid's own "an all-zero/blank row is
  skipped" rule.
- **Strategy identity for de-duplication is the resulting
  `StrategyDefinition` (market_key/offsets/weights), never the Label** —
  a trader's Label is a human-facing description, not an identifier, and
  the same Label can legitimately recur across genuinely different
  strategies.
- **Nothing is written until explicit confirmation.** `parsing`/
  `validation`/`preview` are pure and in-memory; `commit.commit_import()`
  is the sole write boundary, called only from the "Import All" button,
  and only `ImportPreview.importable_candidates` (a candidate with a
  sheet-level error, or zero ready rows, is never saved even partially)
  are persisted via the existing, unmodified `StrategySetRepository.
  save()`.
- Test suite: see the files listed above; run `pytest -q
  tests/test_strategy_import_*.py tests/test_ui_strategy_import*.py`
  for the up-to-date count rather than trusting a number recorded here.

---

## Module 8 – QuantHub Secondary Provider, Provider Provenance & Effective-Request-End

COMPLETED AND TESTED. This module was built across several rounds after
Module 7A and was not previously documented here — this section is
that missing documentation, written from the current code, not from
memory of how it was designed.

Introduces QuantHub as a second market-data provider alongside LSEG,
a persisted per-`(ric, interval)` decision of which provider actually
serves a given contract/interval ("provider provenance"), and a fix
that stops a currently-forming bar from ever being fetched, cached, or
returned. Touches `core/quanthub.py`, `core/providers.py`,
`core/downloader.py` (permission-error classification only),
`database/models.py`, `database/cache.py`, `database/service.py`.
`strategy_engine/`, `template_scanner/`, and `range_analytics/` are
completely unaffected — they still only ever call `database.get_history`/
`get_history_batch`, unaware a second provider exists at all.

core/
    quanthub.py     (build_instrument, download_history,
                     download_history_batch -- QuantHub's own fetch/
                     resample/count-estimation logic)
    providers.py    (Provider enum, PROVIDER_ROUTING market->provider
                     map, resolve_provider(), qh_root_for_market())
    downloader.py   (+ _is_confirmed_no_intraday_permission(), the
                     third LDError classifier -- see below)

database/
    models.py       (+ SyncRange.provider, nullable)
    cache.py         (+ get_established_provider(); record_sync_range()
                     gained a `provider` parameter; delete_bars_and_
                     sync_ranges() reframed as an administrative/reset
                     utility)
    service.py        (get_history()/get_history_batch() rewritten
                     around the provider-provenance state machine and
                     _effective_request_end() -- see below)

connection.py's `init_db()` also gained a small, idempotent, additive
migration (`ALTER TABLE sync_ranges ADD COLUMN provider`) so an
existing local `data/oscill8.db` created before this column existed
gets it added in place, without losing any cached history — unlike the
earlier PriceBar-nullability migration (Module 2/CHANGELOG v0.4.0),
which told users to delete and rebuild their cache, this one must not,
since `sync_ranges` coverage bookkeeping (not just re-fetchable price
data) would otherwise be lost.

### Which markets are QuantHub-mapped

`core.providers.PROVIDER_ROUTING` (the single source of truth — nothing
else in the codebase should hard-code a market→provider decision):

| Market key | QuantHub product (for reference) |
|---|---|
| `CORRA` | CORRA |
| `SONIA` | SONIA 3M |
| `EURIBOR` | Euribor |
| `SARON` | SARON |
| `YBA` | Australia 90 Day Bank Bill |
| `ESTR_ICE` | ESTR (ICE_EUROPE exchange, disambiguated from the CME `ESTR` product name collision) |

Every other market (`SOFR`, `FED_FUNDS`, `ESTR` — the CME product,
distinct from `ESTR_ICE`) defaults to LSEG-only, completely unaffected
by anything in this module — `resolve_provider()` returns `Provider.LSEG`
for any market key not in `PROVIDER_ROUTING`, and the entire provenance
machinery below is skipped for it (see `get_history()`'s own
`has_quanthub_fallback` branch). A market must have BOTH a
`core.config.MARKETS` entry (for LSEG-side RIC construction, used
regardless of which provider ends up serving the data) AND an entry in
`core.providers._MARKET_KEY_TO_QH_PRODUCT` before it can be routed to
QuantHub at all.

### QuantHub's confirmed API limitation — why the cache matters

**Live-tested and confirmed, not assumed.** QuantHub's `/api/v2/ohlc/`
endpoint is NOT a generic historical-database API. It does not accept
`start=`/`end=` (returns HTTP 500), `from=`/`to=` (HTTP 200 but silently
ignored — byte-identical response with or without it), or `offset=`/
`page=`/`cursor=`/`before=` (each tested in isolation, every one HTTP
200 with the exact same window as a baseline request — silently
ignored, never applied). There is no pagination, cursor, offset, or
date-range mechanism of any kind.

The only parameters that have any effect are:

```
instruments=   (comma-separated QH instrument identifiers)
interval=      (native QuantHub interval)
count=         (how many of the MOST RECENT observations, as of now)
```

`count=` always means "the most recent N observations as of when the
request is made" — there is no way to anchor a request to an earlier
reference point. A request whose true required count would exceed the
achievable cap simply retrieves a shorter history than requested,
never multiple requests, never fabricated bars — older history beyond
that is genuinely unreachable in a single request, no matter how the
request is shaped.

**Hard row ceiling, live-confirmed exactly:** `QUANTHUB_MAX_ROWS_PER_REQUEST
= 10_000` (`core/quanthub.py`) — 10,000 total rows in one HTTP request
succeeds; 10,001 returns HTTP 400 "Max row limit exceeded (10000)".
This cap is shared across every instrument in the request:
`instruments_in_request × count ≤ 10,000`. `QUANTHUB_BATCH_SIZE = 10`
is the separate, independently live-verified maximum instrument count
per HTTP request (10 distinct instruments in one request returned all
480 expected records for `count=48`; not tested above 10). Because the
row ceiling is shared, batching MORE instruments into one request
directly SHRINKS the effective `count` (and therefore how far back)
each individual instrument in that request can reach — `core.quanthub.
_max_count_for_batch(batch_size)` computes `10_000 // batch_size` as
the per-instrument cap for whatever batch size is actually used; a
smaller trailing chunk legitimately gets a HIGHER count than a
full-sized batch of 10.

**This is exactly why the SQLite cache matters more for QuantHub-backed
instruments than for LSEG-backed ones.** A cold-started QuantHub
instrument can only ever receive, on its first-ever fetch, the most
recent history reachable within that request's effective count cap —
nothing older is retrievable through this endpoint, ever, regardless of
how the request is shaped. Deep history is NOT permanently unreachable,
though: Module 2's SQLite cache persists every completed bar QuantHub
returns and never re-fetches what it already has, so an instrument that
gets scanned repeatedly over time accumulates history day by day as
"now" (and therefore QuantHub's own reachable window) advances — this
is the existing, unmodified caching behavior already doing the only
thing that can compensate for QuantHub's API-side ceiling; it does not
change the ceiling itself for a genuinely new instrument's first
request.

### Provider provenance: the state machine

Persisted on `database.models.SyncRange.provider` (nullable `"LSEG"` /
`"QUANTHUB"` / `NULL`), keyed on `(ric, interval)` TOGETHER, never on
the ric alone — the same contract can legitimately have a different
established provider at a different interval (e.g. `SONH26` DAILY
established LSEG, `SONH26` HOURLY established QUANTHUB).
`database.cache.get_established_provider(session, ric, interval)` reads
it; `record_sync_range(..., provider=...)` writes it (merging with any
overlapping existing row — the merged row always takes the INCOMING
call's provider, since every row for one `(ric, interval)` is
guaranteed to already share the same provider once established).

Four states, decided in `database/service.py`'s `get_history()` /
`_get_history_batch_with_provenance()`:

```
                         Genuinely new (ric, interval)
                     (no established provider, no sync_ranges
                      coverage at all -- nothing ever cached)
                                    |
                                    v
                     Try LSEG for the FULL requested window
                                    |
                     +--------------+--------------+
                     |                             |
              complete / usable            unavailable / incomplete
           (_is_complete_history)             (or empty response)
                     |                             |
                     v                             v
            LSEG bars persisted;             LSEG attempt discarded
          provider ESTABLISHED "LSEG"        entirely, never persisted;
        (this completeness test runs        QuantHub fetched for the
         EXACTLY ONCE per (ric, interval),   full window instead;
         never repeated again)              provider ESTABLISHED
                                             "QUANTHUB"
```

```
                      Legacy/unknown (ric, interval)
                (no established provider, BUT sync_ranges
                 coverage already exists -- e.g. cached before
                 the provider column existed, migrated to NULL)
                                    |
                                    v
                    For each genuinely missing sub-range:
                          Try LSEG for that sub-range
                                    |
                     +--------------+--------------+
                     |                             |
              usable / complete            unavailable (confirmed
                                          MarketDataUnavailableError)
                                             / empty / incomplete
                     |                             |
                     v                             v
             Persisted, provider           QuantHub fetched for that
             STAYS "unknown"               sub-range as a fallback;
             (provider=None,                STILL persisted with
             explicitly recorded,           provider=None -- NEVER
             every time)                    "QUANTHUB", no matter
                                            which provider actually
                                            served this sub-range
```

Once established (LSEG or QuantHub), the decision is NEVER
automatically revisited — `established == "LSEG"` fetches ONLY the
missing sub-range(s) from LSEG forever after (QuantHub is never
consulted again for that `(ric, interval)`); `established == "QUANTHUB"`
re-requests the FULL effective window from QuantHub whenever ANYTHING
is missing (QuantHub's own API limitation, described above — it cannot
be asked for just a narrow gap), and LSEG is never consulted again
either. **A `(ric, interval)`'s history is never a mix of LSEG and
QuantHub bars for a single established provider** — but see the
LEGACY/UNKNOWN case below, which is the one deliberate, narrow
exception to that rule.

**LEGACY/UNKNOWN is a genuinely different, fourth state, not a variant
of establishment.** `get_established_provider()` returning `None` is
ambiguous by itself — it means EITHER "genuinely never touched" OR
"touched, but before provider provenance existed as a concept."
`get_history()`/`get_history_batch()` disambiguate by ALSO checking
whether `sync_ranges` coverage already exists: non-empty means LEGACY/
UNKNOWN (`_fetch_legacy_unknown_provider()` in `database/service.py`
applies); empty means genuinely new (the establishment flow above
applies). For a legacy row, a `(ric, interval)`'s bars CAN legitimately
have come from either provider across different sub-ranges over time —
that's accepted, not fixed, because the row's true original provenance
(before this design existed) is unrecoverable and must never be
guessed at. `provider` stays `NULL` permanently for such a row; only
`cache.delete_bars_and_sync_ranges()` (an explicit administrative/reset
utility, not part of normal retrieval — see its own docstring) can
clear a `(ric, interval)`'s cache/provenance so the next request
performs fresh, explicit establishment.

**A real production bug this design fixes:** an earlier version of the
legacy/unknown path left any LSEG failure uncaught, on the theory that
falling back to QuantHub might risk fabricating QuantHub provenance.
That reasoning didn't hold once `provider=None` is recorded on every
branch regardless of which provider actually served the data — and
leaving it uncaught meant a single confirmed-unavailable or empty LSEG
response for a legacy RIC aborted the entire scan instead of degrading
gracefully, exactly as every other QuantHub-mapped state already did.
Live-observed in production for `CRAU7 [4H]`.

### How LSEG responses are classified

`core/downloader.py`'s `_fetch_chunk()` is the ONE place an LSEG
`LDError` gets turned into the typed `MarketDataUnavailableError` (or
doesn't) — every caller above it only ever sees `MarketDataUnavailableError`,
a plain `pd.DataFrame`, or an unrecognized exception that propagates:

| LSEG outcome | What `download_history()` does | Retried by tenacity? |
|---|---|---|
| Valid data returned | Returns a normal, non-empty DataFrame | n/a |
| Successful call, 0 bars | Returns an EMPTY DataFrame (correct columns), NOT an exception — logged as a warning | n/a |
| `TS.Interday.UserRequestError.70005` ("The universe is not found") | Raises `MarketDataUnavailableError` | No — excluded from retry before the predicate ever sees it |
| `TS.Interday.UserNotPermission.70112` ("User does not have permission for this universe") | Raises `MarketDataUnavailableError` | No |
| `*.UserNotPermission.92000` (any service/product prefix — live-confirmed in production as BOTH `TS.Intraday.UserNotPermission.92000` and `TSCC.QS.UserNotPermission.92000` for the same underlying condition, on top of LSEG's own wording for this code also varying — `"User has no permission"` is what a real request actually returned, not the phrase originally assumed) | Raises `MarketDataUnavailableError` | No |
| Any other `LDError`, or any other exception (network/auth/an unrecognized LSEG error, a programming bug) | Propagates UNCHANGED, as whatever type it originally was | Yes — ordinary retry-with-backoff behavior, unaffected |

Three narrow, exact-match, duck-typed classifiers do this work —
`_is_confirmed_universe_not_found()`, `_is_confirmed_no_permission()`,
`_is_confirmed_no_intraday_permission()` — each requiring the exception
be LSEG's actual `LDError` type (`module="lseg.data._errors"`,
`class="LDError"`) plus its specific error code. This is deliberately
NOT a broad "any `LDError` means unavailable" catch — an unrelated or
unrecognized LSEG error is never silently treated as a QuantHub
fallback condition anywhere in the codebase; only these three exact,
live-confirmed codes are.

Once `MarketDataUnavailableError` reaches `database/service.py`, an
EMPTY or INCOMPLETE-but-non-exception response is treated identically
to that exception for provider-decision purposes: `_is_complete_history()`
(used both in establishment and in the legacy/unknown fallback) returns
`False` for an empty frame or one with an interior gap wider than a
generous business-day threshold — so "LSEG raised `MarketDataUnavailableError`"
and "LSEG returned successfully but with unusable data" both route into
the exact same QuantHub-fallback branch, not two separate code paths.

### Effective request end — currently-forming bars are never fetched, cached, or returned

`database/service.py`'s `_effective_request_end(end, boundary)` caps
every request/coverage check to the last FULLY CLOSED bar for the
requested interval, computed fresh from `_last_completed_boundary
(interval, now)` (unchanged — the start of the bar currently forming
at `now`, e.g. for a 4H interval at 15:47, the boundary is 12:00, and
the bar dated 12:00 -- spanning `[12:00, 16:00)` -- is still forming
and therefore excluded; the last CLOSED bar is the one dated 08:00).
Applied independently, before any cache-coverage check or provider
call, in `get_history()`, `_get_history_batch_with_provenance()`, and
`_get_history_batch_quanthub()` — each already independently computed
its own `now`/`boundary`, so this introduces no new cross-function
coupling. Applies identically across DAILY, HOURLY, and 4H, and to
every provider state (LSEG-only, established LSEG, established
QuantHub, legacy/unknown, and fresh establishment) — capping happens
BEFORE the provider branch is even chosen.

**The real problem this fixes:** a plain-date request (e.g. "today")
coerces to day-end (`23:59:59.999999`) via `_coerce_end()`, and that
previously stayed uncapped all the way through `_missing_ranges()` and
into the provider request itself. A scan for a still-forming interval
therefore re-requested the same partial bar from the provider on EVERY
identical re-scan, since nothing in that wide, always-in-the-future
tail could ever be marked synced. Capping the effective end BEFORE
`_missing_ranges()` runs means the gap closes to nothing once the last
closed bar is already cached, so a second identical scan makes ZERO
further provider requests during the same still-forming period; the
very next request after the period actually closes fetches EXACTLY
that one newly-closed bar, nothing more.

A currently-forming bar is consequently never returned to any caller
either (an earlier version of this design DID still return it, never
cached, for that one call only — that behavior is gone; see the
now-superseded Module 2 bullet above). `_persist_downloaded()`'s
`Date < boundary` filter is the actual, load-bearing enforcement point
for this — not a redundant safety net: QuantHub in particular has no
way to be asked, server-side, to exclude a same-day in-progress bar (its
`count=`-only API always means "the most recent N observations as of
now," and its own local response filter truncates the request's `end`
to a bare calendar date before filtering), so this Date-level check is
what actually keeps such a bar out of the cache regardless of what
either provider hands back.

If `effective_end < start` (the ENTIRE requested window is still
forming — e.g. a narrow intraday request wholly inside the currently-
open 4H bucket), no provider is contacted at all, for anyone, and a
genuinely-new QuantHub-mapped ric's one-time establishment test is
correctly skipped rather than run against a window that could never
produce a usable result — this is not merely "returns fewer rows," it
is "zero provider calls, and no false establishment decision."

### `prewarm_leg_cache()` / `build_history()` — no duplicate fetch

`strategy_engine.pricing.prewarm_leg_cache(instances, price_start,
price_end)` batches every distinct leg RIC across `instances` (grouped
by interval, since QuantHub's batching happens per-interval) into as
few `database.get_history_batch()` calls as possible, and populates a
`LegCache` dict keyed EXACTLY as `_fetch_leg()`/`build_history()`
already expect: `(ric, interval.value, str(price_start), str(price_end))`.
`_fetch_leg()` is a plain `if key not in leg_cache: fetch` check with no
provider awareness at all — as long as prewarm populated the key (which
it does for every ric `get_history_batch()` returns, across all four
provider states above), `build_history()` never re-fetches it. This
module (`strategy_engine/`) never imports `core.downloader`/
`core.quanthub`/`core.providers` directly and has no idea a second
provider or a provenance decision exists — all of that lives entirely
inside `database.service`.

### Debugging gotcha: LSEG RIC vs. QuantHub instrument identifier are DIFFERENT cache keys

`database.cache.get_established_provider()`/`get_sync_ranges()` are
keyed EXCLUSIVELY on the LSEG RIC string (e.g. `CRAU6` — CORRA's
1-digit-year convention), the same string `strategy_engine`/
`template_scanner` pass into `database.get_history()` everywhere. The
QuantHub instrument identifier built for the actual HTTP call (e.g.
`CRAU26` — QuantHub's own, independent 2-digit-year convention, see
`core.quanthub.build_instrument()`) is NEVER used as a cache/sync_ranges
key anywhere — it only ever appears inside the outbound QuantHub
request itself. Querying `sync_ranges`/provenance by the QuantHub
instrument string (e.g. filtering on `ric='CRAU26'`) will always show
"no coverage," regardless of what is actually cached under the real key
(`CRAU6`) — this cost real debugging time investigating an apparent
QuantHub `count=` discrepancy that turned out to be a cache lookup
against the wrong identifier. Always use the LSEG RIC when inspecting
`sync_ranges`/provider provenance directly (e.g. via SQL or
`database.cache`), never the QuantHub instrument string.

### Debugging gotcha: a QuantHub `count=` observed at different times can legitimately differ

For a LEGACY/UNKNOWN-provider `(ric, interval)` (see above), `count=`
is computed by `core.quanthub._estimate_count()` from whatever
`sync_ranges` coverage's trailing edge actually is AT THE MOMENT OF THAT
CALL — not from the strategy's originally-configured price window, and
not remembered across calls (`record_sync_range()` merges coverage
forward; it does not retain history of where the edge used to be). A
long-requested window (e.g. `2026-01-01 -> 2027-08-01`) that is mostly
already cached will correctly compute a SMALL `count` sized to just the
narrow, genuinely-missing trailing gap — a `count` far smaller than the
window's own nominal span is expected, correct behavior for this path,
not a bug, a hardcoded cap, or a sign the date-window logic is broken.
Live-investigated end-to-end (empty cache -> full-window count;
today's-actual partial cache -> tail-only count) with no code defect
found; see `tests/test_service_provider_fallback.py`'s
`test_m_a_legacy_cache_missing_tail_fetches_only_the_gap_provider_stays_null`
for the same class of scenario under test.

### Test invariants (guarantees the current suite locks in, not exhaustive)

- A genuinely new QuantHub-mapped `(ric, interval)` tries LSEG first;
  complete data establishes LSEG, incomplete/unavailable establishes
  QuantHub — exactly once, never re-decided.
- An established `(ric, interval)` never mixes providers; established
  LSEG never calls QuantHub even when QuantHub could technically serve
  the request, and vice versa.
- Provider state is independent across intervals for the SAME contract
  (`SONH26` DAILY and `SONH26` HOURLY can be established differently).
- A legacy/unknown row's provider is NEVER fabricated as LSEG or
  QuantHub, regardless of which provider actually serves a given
  missing sub-range, and regardless of what other RICs in the same
  batch call are doing.
- `*.UserNotPermission.92000` — in either live-confirmed real-world form,
  `TS.Intraday.UserNotPermission.92000` OR `TSCC.QS.UserNotPermission.92000`
  (prefix-agnostic; the classifier matches on the stable
  `"UserNotPermission.92000"` substring with a trailing-digit boundary
  guard, never a hardcoded prefix) — is classified as
  `MarketDataUnavailableError` and is NOT retried by tenacity
  (`call_count == 1`, no retry storm) — proven both at the
  `core.downloader` classifier level and at the `database.service`
  legacy-fallback level.
- An unrelated or unrecognized `LDError` is never silently converted
  into a QuantHub fallback, at any layer.
- A repeated, identical scan during the SAME still-forming period makes
  ZERO additional provider requests, for DAILY, HOURLY, and 4H alike,
  and for every provider state; the very next request after the period
  closes fetches exactly the newly-closed bar.
- A request window falling ENTIRELY inside the currently-forming period
  triggers zero provider calls and no false establishment, even for a
  genuinely new QuantHub-mapped ric.
- A RIC/leg already resolved by `prewarm_leg_cache()` is never
  re-fetched by a subsequent `build_history()` call for the same
  instances/window, across all four provider states in the same batch.

See `tests/test_downloader.py`, `tests/test_service_provider_fallback.py`,
`tests/test_service_effective_request_end.py`,
`tests/test_service_get_history_batch.py`,
`tests/test_multimarket_cache_key_independence.py`,
`tests/test_intermarket_strategy_set_provider_routing.py`,
`tests/test_strategy_pricing.py`, `tests/test_quanthub.py`.

**Deferred / open, not solved here:**
- CORRA's LSEG entitlement gap (`70112`) and the other three
  trader-confirmed-but-not-yet-live-LSEG-verified markets (EURIBOR,
  SARON, ESTR_ICE — see the Module 1 CORRA note and the roadmap's
  EURIBOR resolution above) remain exactly that: unresolved at the LSEG
  entitlement layer. The QuantHub fallback means a scan still gets
  usable data for these today; it does not mean LSEG access has been
  restored or that `verified=True` should be set for them.
- No mechanism exists to force-migrate a LEGACY/UNKNOWN row to an
  explicit provider without going through `cache.delete_bars_and_
  sync_ranges()` (full reset + fresh establishment) — there is no
  narrower "just tell me you're actually LSEG" operator command.
- QuantHub instrument-suffix month codes are live-verified only for the
  "H" (March) code across 6 real examples; the other eleven letters are
  carried over from the universal futures-industry convention, not
  independently confirmed against the live API (see `core/quanthub.py`'s
  own module docstring).

### Environment configuration: `.env` loading (`ui/app.py` fix)

`ui/app.py` (`streamlit run ui/app.py`) previously never loaded a
`.env` file — every `RBS_*` setting (`RBS_QUANTHUB_TOKEN`,
`RBS_LSEG_APP_KEY`, etc.) was visible to `core.config` only if already
present as a real OS/session environment variable at Streamlit startup.
`python-dotenv` was already a pinned `requirements.txt` dependency but
was wired into nothing in the actual application entry point — a real,
live-observed gap: a user with `RBS_QUANTHUB_TOKEN` set only in a local
`.env` file saw a working `python` script that called `load_dotenv()`
itself, but got `QuantHubCredentialsMissingError` from the live
Streamlit app for the exact same market. Fixed with two lines
(`from dotenv import load_dotenv` / `load_dotenv()`) placed immediately
after `ui/app.py`'s existing `sys.path` bootstrap and before any
`from ui...`/`from core...` import that transitively imports
`core.config` (which reads `RBS_*` vars at module-import time, so
`load_dotenv()` must run first). No other file changed — `core.config`/
`core.quanthub` already read `RBS_*` via plain `os.environ.get()`;
they simply needed the environment populated before their first import.

---

## Module 9 – Intermarket Strategy Engine (Domain Model, Strategy Set Integration, Scanner Wiring)

COMPLETED AND TESTED (backend only — see "Not yet done" below). Adds
the ability for a SINGLE strategy to combine legs from DIFFERENT
markets (e.g. a SOFR leg + a SONIA leg priced into one series), as an
**additive sibling** to the existing single-market
`StrategyDefinition`/`StrategyInstance` — that path is completely
unmodified, and `strategy_engine/combinations.py`/`pricing.py` are
untouched. This is distinct from Module 7A's existing "multi-market"
support (several separate single-market `StrategySetEntry`s grouped in
one `StrategySet`) — that groups independent single-market strategies;
this module lets ONE strategy's own legs span multiple markets.

Built and reviewed in two phases; a third hardening pass fixed one real
bug before the combined work was committed.

strategy_engine/
    intermarket_definitions.py    (LegSpec, IntermarketDefinition,
                                   resolve_display_market_key(),
                                   resolve_display_offsets())
    intermarket_combinations.py   (IntermarketStrategyInstance,
                                   generate_intermarket_instances())

range_analytics/
    units.py       (+ BpConversionUnavailable, resolve_bp_per_point())
    results.py     (+ RangeAnalytics.bp_per_point field)
    multi_lookback.py (uses result.bp_per_point, not a market_key re-lookup)

template_scanner/
    scanner.py     (analyze_histories()/run_scan_on_instances() use the
                    display resolvers; zero new market-specific logic)
    scan_results.py (ScanCandidateResult.instance type hint widened only)
    universe.py    (+ dedupe_intermarket_candidates(), a sibling to the
                    existing, untouched dedupe_candidates())

strategy_sets/
    model.py         (+ IntermarketStrategySetEntry,
                      StrategySet.intermarket_entries)
    serialization.py (+ leg_to_dict/_from_dict,
                      intermarket_entry_to_dict/_from_dict; entries are
                      routed by "legs" key presence, never a type tag)
    expansion.py     (expand_strategy_set() gained a second loop over
                      intermarket_entries -> generate_intermarket_instances())
    execution.py     (with_interval_override() fixed to also rebuild
                      intermarket_entries — see the bug note below)

test_intermarket.py (repository root — see its own section below)

tests/
    test_intermarket_definitions.py, test_intermarket_combinations.py,
    test_intermarket_pricing_compatibility.py,
    test_intermarket_strategy_set_end_to_end.py,
    test_intermarket_strategy_set_provider_routing.py, plus extensions
    to test_range_units.py, test_range_analytics.py,
    test_range_multi_lookback.py, test_strategy_sets_model.py,
    test_strategy_sets_serialization.py, test_strategy_sets_expansion.py,
    test_strategy_sets_execution.py, test_template_scanner_universe.py,
    test_template_scanner_scanner.py.

Key design points a future session needs:

- **Zero market-specific logic, anywhere.** No file in this module
  contains an `if market_key == "..."` (or strategy-shape) branch —
  every dispatch between single-market and intermarket is by TYPE
  (`isinstance(definition, IntermarketDefinition)`), never by value.
  SOFR/SONIA/CORRA in any docstring or test are illustrative example
  data only, never special-cased.
- **`LegSpec(market_key, offset, weight)`** bundles a leg's three
  fields together (not three parallel tuples the way
  `StrategyDefinition` does) — a mismatched-array-length failure mode
  is structurally impossible here. `IntermarketDefinition(legs,
  interval, price_field="Close", bp_per_point=None)` validates: ≥1 leg,
  all elements are `LegSpec`, `min(offsets) == 0` (at least one leg
  anchors the window), not all weights zero, valid interval/price_field,
  `bp_per_point > 0` if given. Unlike `StrategyDefinition.offsets`,
  `LegSpec.offset` values MAY repeat across legs (two different
  markets' legs both at `offset=0` is the ordinary intermarket-spread
  case).
- **Offset semantics (design-reviewed — "interpretation A → C"):** a
  `LegSpec.offset` is ALWAYS a position on THAT LEG'S OWN contract
  curve, exactly like `StrategyDefinition.offsets`'s existing meaning
  for a single-market strategy — NEVER a position on a curve shared/
  intersected across all legs. `generate_intermarket_instances()`:
  (1) splits legs into anchor legs (`offset == 0`) and non-anchor legs;
  (2) intersects ONLY the anchor legs' own independently-generated
  `(year, month)` curves (via `core.futures_calendar.generate_contracts()`
  + `core.ric.parse_ric()`, both unmodified) to find valid anchor
  periods; (3) for each non-anchor leg, generates its OWN full curve
  independently (never intersected with anything), finds the first
  position at or after the anchor period via `bisect_left`, then steps
  forward `offset` MORE positions on that SAME curve. If a non-anchor
  leg's own curve doesn't reach that far, that anchor period simply
  produces no instance (mirrors `combinations.generate_instances()`'s
  own "too few contracts" behavior) — never an error. An earlier draft
  intersected ALL legs' calendars before applying offsets, which
  silently discarded real contracts on a finer-grained market's own
  curve whenever paired with a coarser one; this was corrected before
  Phase 1 was committed. Internally always sorts/compares `(year,
  month)` tuples (not `(month, year)`) so ordering is correct across a
  year boundary; `core.ric.build_ric()`'s own `(market_key, month,
  year)` argument order is only reconstructed at the point of RIC
  construction.
- **`IntermarketStrategyInstance(definition, rics)`** deliberately
  mirrors `StrategyInstance`'s exact field names/shapes — this is what
  lets `strategy_engine.pricing.build_history()`/`prewarm_leg_cache()`
  consume it with ZERO code change: neither function reads
  `market_key` anywhere, only `instance.rics` and
  `instance.definition.{interval, price_field, weights}`, and
  `IntermarketDefinition` exposes `weights`/`market_keys` as computed
  properties for exactly this reason.
- **Display-only vs. authoritative identity.**
  `resolve_display_market_key()`/`resolve_display_offsets()`
  (`strategy_engine/intermarket_definitions.py`) produce cosmetic
  composite labels (e.g. `"SOFR/CORRA"`, offsets in leg order) for
  scanner result tables/summaries ONLY — dispatched purely by type.
  These values must NEVER be used for provider resolution
  (`core.providers.resolve_provider`), cache/database lookup, QuantHub/
  LSEG instrument mapping, or bp conversion — all of those still
  operate strictly per-RIC or per-`LegSpec.market_key`, never via a
  composite label. `template_scanner.scanner`'s own
  `test_scanner_module_remains_unaware_of_strategy_sets` design-
  principle test (the string `"strategy_sets"` must never appear in
  that module's source) was preserved throughout this work.
- **bp-conversion: explicit, never guessed.**
  `range_analytics.units.resolve_bp_per_point(definition)` dispatches
  by type: a single-market `StrategyDefinition` resolves via the
  existing per-market registry lookup (unchanged); an
  `IntermarketDefinition` uses its own explicit `bp_per_point` override
  if set, otherwise raises `BpConversionUnavailable` — there is no
  principled way to pick "the" market whose bp convention applies to a
  genuinely cross-market series, so it is never inferred from any one
  leg. `RangeAnalytics.analyze_range()` catches this specific exception
  and leaves `vol_bp` (and other bp-derived fields) as `NaN` rather than
  aborting the whole scan — a deliberate, disclosed design decision:
  unavailable-bp-conversion degrades that one metric, never the scan.
  `range_to_volatility_ratio()` (Module 4B) reads `result.bp_per_point`
  directly rather than re-deriving it from a (possibly composite)
  display market key, which would silently break for an intermarket
  result.
- **Candidate identity = the REALIZED instance.** `dedupe_intermarket_
  candidates()` (`template_scanner/universe.py`, a sibling to the
  existing, untouched `dedupe_candidates()`) treats two candidates as
  identical iff their RICs, weights, interval, and price_field all
  match — never the abstract legs/offsets that generated them — same
  philosophy as the single-market dedup.
- **Strategy Set schema: discriminated by key presence, never a type
  tag.** A single-market entry's JSON is flat
  (`market_key`/`offsets`/`weights`/...); an intermarket entry has a
  `"legs"` array instead. `strategy_set_from_dict()` routes each raw
  entry purely by `"legs" in raw_entry` — never an explicit
  `"type"`/`"kind"` field, never inferred from the entry's name.
  `IntermarketStrategySetEntry` mirrors `StrategySetEntry` (`name`,
  `definition`, `enabled`) but rejects `expansion.max_curve_position`
  at construction (that field is single-market-curve-specific and has
  no intermarket analogue today). `StrategySet.intermarket_entries:
  tuple[IntermarketStrategySetEntry, ...] = ()` is an additive,
  default-empty field alongside the existing `entries` — a `StrategySet`
  may contain only single-market entries, only intermarket entries, or
  a genuine mix; entry names must be unique across BOTH collections
  combined, validated in one `__post_init__` pass.
- **The contract-selection window is call-time-only** — same
  precedent as Module 7A's own `entries`: `expand_strategy_set()`
  takes `contract_start`/`contract_end` as call-time arguments shared
  across every entry (single-market AND intermarket), never baked into
  the saved JSON, so a saved set never goes stale as "today" moves.
- **Ordering note (documented, not "fixed"):** `expand_strategy_set()`'s
  raw/unranked output is always single-market instances first, then
  intermarket instances — never JSON interleave order — because it
  runs two separate loops (one per collection) and concatenates. This
  is inconsequential in practice: the live UI's `ui.results_view.
  _current_rank_state()` always applies a default rank key before
  display, so raw expansion order is never what a user actually sees.
- **Real bug found and fixed during the hardening pass:**
  `strategy_sets/execution.py`'s `with_interval_override()` originally
  rebuilt only `strategy_set.entries` under the new interval, silently
  leaving `intermarket_entries` at their ORIGINAL interval. Fixed to
  rebuild both collections. Verified via `git stash` that the added
  regression test genuinely failed without the fix and passed with it.
- **`strategy_sets/execution.py` is not reachable from any live UI
  button today** — confirmed by tracing the two separate scan entry
  points that exist: "MANUAL GRID" (`ui` → `template_scanner.
  run_scan()`) and "STRATEGY SET" (`expand_strategy_set()` →
  `run_scan_on_instances()`, which is what `execution.py` wraps). This
  was true before this module's work and remains true after it — this
  module did not change UI wiring at all (see "Not yet done" below).

**Not yet done / explicitly out of scope for this module:**
- **No Streamlit UI support for creating or editing intermarket
  entries.** A `StrategySet` containing `intermarket_entries` can only
  be authored by hand-editing its JSON file directly (or a script) —
  there is no editor panel. If such a JSON is loaded through the
  existing Strategy Set UI panel, its intermarket entries are silently
  not represented in that panel's single-market-only editing grid (they
  are not corrupted or lost in the file — simply not shown/editable
  there).
- No UI button currently triggers `expand_strategy_set()` →
  `run_scan_on_instances()` end-to-end for a live user — see the
  `execution.py` note above. Wiring this in is a distinct, unstarted
  follow-up.
- Real-time provider/data behavior (LSEG vs. QuantHub per leg, caching,
  provenance) is entirely Module 8's existing, unmodified concern —
  this module never imports `core.downloader`/`core.quanthub`/
  `core.providers`/`database` directly; every leg still goes through
  `strategy_engine.pricing.build_history()` → `database.get_history()`
  exactly like a single-market leg.

### `test_intermarket.py` — standalone manual validation harness

A TEMPORARY, standalone script at the repository root (NOT a pytest
file, despite its `test_` prefix — run directly via `python
test_intermarket.py`) that independently verifies, against real LSEG/
QuantHub data, that (a) each generated leg RIC corresponds exactly to
the `IntermarketDefinition` leg that produced it (via `core.ric.
parse_ric()`, by position, never inferred from the strategy's name or
shape), and (b) `Strategy = sum(leg_price_i * leg_weight_i)` matches
Oscill8's own computed `Strategy` column exactly (1e-10 tolerance).

Loads a named `StrategySet` via `StrategySetRepository().load(name)`
and iterates `strategy_set.intermarket_entries` INDIVIDUALLY — it
deliberately never calls `expand_strategy_set()` on the whole set
(which would flatten and dedupe every entry's instances together,
losing which entry produced which instance) and never goes through
`template_scanner`/`ScanReport` (it calls `strategy_engine.pricing.
build_history()` directly, validating the lower-level pricing
calculation, not the scanner pipeline built on top of it). Calls
`load_dotenv()` before any Oscill8 import, same requirement as
`ui/app.py` above. Test parameters (`STRATEGY_SET_NAME`,
`CONTRACT_START`/`CONTRACT_END`, `PRICE_START`/`PRICE_END`,
`TOLERANCE`) are declared as plain module-level constants at the top of
the file. Prints per-leg RIC/mapping tables, a contribution table, the
manual calculation expression, and a final PASS/FAIL summary; handles a
missing StrategySet file or an empty/unbuildable history gracefully
(no crash). Does not modify production code or the StrategySet JSON,
and never prints credential/token values. Not part of the pytest suite
and not intended to become a permanent module — a genuinely temporary
developer tool, kept in the repo for now at explicit request.

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

**This diagram describes the original, LSEG-only design and is still
accurate for any market not routed to QuantHub** (see
`core.providers.PROVIDER_ROUTING` — today that's SOFR, FED_FUNDS, and
the CME `ESTR` entry). For the six markets routed to QuantHub (CORRA,
SONIA, EURIBOR, SARON, YBA, ESTR_ICE), the real behaviour is richer
than this diagram shows — a persisted per-`(ric, interval)` provider
decision, QuantHub full-window refetches instead of incremental
missing-range downloads, a legacy/unknown fallback path, and an
effective-request-end cap that excludes the currently-forming bar. See
**Module 8 – QuantHub Secondary Provider, Provider Provenance &
Effective-Request-End** above (and `database/service.py`'s own module
docstring, which is the authoritative source) for the accurate,
current end-to-end flow. This diagram is kept here for historical
context, not updated to show QuantHub, since the two provider flows
are shaped too differently to merge into one diagram without losing
clarity.

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
Module 6A — Streamlit range-bound scanner UI (incl. the Range-Bound
Opportunities Market filter, column selector, and Strategy Label
enhancement) — STATUS: COMPLETE
Module 6B — Selected-strategy history chart — STATUS: COMPLETE
Module 7A — Strategy Set engine (domain model, JSON persistence,
expansion to StrategyInstance[]) — STATUS: COMPLETE
Module 7B — Strategy Set UI integration (selector built into the
scanner grid, simplified single-grid design) — STATUS: COMPLETE
Strategy Set Import — Excel/CSV -> StrategySet import pipeline and UI
panel — STATUS: COMPLETE
Module 8 — QuantHub secondary provider, provider provenance, and
effective-request-end (currently-forming-bar exclusion) — STATUS:
COMPLETE
Module 9 — Intermarket strategy engine (domain model, Strategy Set
integration, scanner wiring; cross-market legs within ONE strategy) —
STATUS: COMPLETE (backend only — no Streamlit UI editor and no live
UI button triggers the richer per-entry Strategy Set execution path
yet; see Module 9's own "Not yet done" notes above)

Current suite: re-run `pytest -q` for the up-to-date count, do not
trust any number written here blindly — see README.md's Testing
section. As of this documentation pass: 1290 passed, 1 known
pre-existing environment-specific failure
(`tests/test_cache.py::test_read_bars_output_matches_downloader_
canonical_schema`, a `datetime64[us]` vs `datetime64[ns]` pandas
version mismatch, not a real bug), 2 skipped
(`tests/test_ui_keyboard_browser.py` — no playwright installed;
`tests/test_quanthub_live_smoke.py` — `RBS_QUANTHUB_TOKEN` not set) —
1293 total.

Deferred / not yet implemented (do not assume any of these exist merely
because they're listed here as being considered):

- Z-score / current-dislocation analytics distinct from the existing
  `RangeAnalytics.z_score`/`abs_z_score` fields (both implemented and
  shown in the UI's `Z`/`|Z|` columns today) — a SEPARATE robust
  Z-score intended specifically to distinguish "quality of historical
  range-boundedness" from "current distance from equilibrium" has no
  approved statistical definition and is not implemented.
- Streamlit UI support for authoring/editing intermarket Strategy Set
  entries, and wiring the richer per-entry Strategy Set execution path
  (`strategy_sets/execution.py`, `expand_strategy_set()` ->
  `run_scan_on_instances()`) into a live UI button — Module 9's backend
  (domain model, persistence, scanner/analytics integration) is
  complete; only the UI surface remains (see Module 9 above). Cross-
  market legs within a single strategy are otherwise implemented, not
  a still-open design question. This is distinct from Module 7B's
  single-market Strategy Set UI, which IS wired into the live scanner
  today (via the simplified grid mechanism, not this richer path).
- An explicit "Real Contract" scanning mode (pick one specific set of
  dated contracts rather than a rolled template) — the backend
  primitives it would need already exist, including an instances-in/
  `ScanReport`-out entry point (`template_scanner.scanner.
  run_scan_on_instances()`, added for Module 9), but no UI surface
  calls it for this purpose today.
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
  only; it does not capture a price window, lookbacks, or results).
- Cloud/server deployment and any non-desktop LSEG authentication.
- ~~EURIBOR market metadata not yet supplied~~ — **RESOLVED, no longer
  deferred.** `core.config.MARKETS["EURIBOR"]` now has a complete,
  trader-confirmed `MarketDefinition` (`ric_root="FEI"`,
  `exchange="ICE_EUROPE"`, `bp_per_point=100.0`, `ric_year_digits=1`,
  `verified=False` since it hasn't been live-LSEG-tested in this
  environment) — see `core/config.py`. EURIBOR is also routed to
  QuantHub (see the QuantHub/Provider-Provenance module below), along
  with three further trader-confirmed-but-not-yet-live-LSEG-verified
  markets added the same way: SARON, YBA (Australia 90-Day Bank Bill),
  and ESTR_ICE (ICE Europe €STR — a distinct market key from the
  existing CME `ESTR` entry, deliberately never collapsed with it).
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