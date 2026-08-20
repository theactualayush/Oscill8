# Oscill8 — Range-Bound Strategy Scanner

Oscill8 is an internal quantitative research application for discovering
range-bound relative-value opportunities across global interest-rate
futures markets (SOFR, Fed Funds, SONIA, CORRA, and Eurozone STIR / €STR).

The system generates multi-leg strategy candidates (outrights, spreads,
flies, condors, and arbitrary custom weight/offset shapes), builds their
historical price series, measures range-bound behaviour, and lets a
trader filter and rank candidates through a compact scanner grid before
drilling into a chosen strategy's own historical chart.

## Architecture

```
LSEG Workspace
      ↓
LSEG Downloader             (core/)
      ↓
SQLite Cache                 (database/)
      ↓
Strategy Engine               (strategy_engine/)
      ↓
Range-Bound Analytics          (range_analytics/)
      ↓
Template / Scanner Engine       (template_scanner/)
      ↓
Streamlit UI                     (ui/)
```

Only the data/downloader layer (`core/`) talks to LSEG. Every layer above
it operates on normalized Pandas DataFrames and has no LSEG dependency —
enforced by tests, not just convention. The UI layer is intentionally
thin: it never computes analytics, never duplicates filtering/ranking/
derived-metric formulas, and never talks to LSEG directly — it only
translates user input into calls against `template_scanner`'s public API
and formats that API's output for display.

## Completed modules

- **Module 1 — LSEG Data Layer** (`core/`): `download_history(ric, interval,
  start, end)` pulls historical OHLCV bars from LSEG Workspace, with RIC
  construction/parsing (`ric.py`), the futures calendar (`futures_calendar.py`),
  and the market registry (`config.py`). Confirmed LSEG error code
  `TS.Interday.UserRequestError.70005` ("The universe is not found") is
  translated into a typed `MarketDataUnavailableError`, distinct from a
  transient network/session error (still retried) or a valid RIC with no
  bars in range (returns empty, not an exception).
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
  diagnostics for a `StrategyHistory` over a selected window (see
  [Range-Bound Metrics](#range-bound-metrics) below for exact definitions).
- **Module 4B — Multi-Lookback / Stability Analytics** (`range_analytics/`):
  `analyze_multi_lookback()` re-runs Module 4A's measurements across
  multiple lookback windows and describes how they move relative to each
  other (dispersion, short-vs-long change, step structure).
- **Module 5A — Template / Candidate Universe Engine** (`template_scanner/`):
  `template_from_dense_weights()` translates a dense grid-style weight
  vector into a `StrategyDefinition`; `generate_candidates()` /
  `generate_candidate_universe()` roll one or many templates across a
  market's eligible contracts into a deduplicated universe of candidate
  `StrategyInstance`s. See [Templates](#templates-and-candidate-generation)
  below.
- **Module 5B — Scanner Orchestration** (`template_scanner/`): `run_scan()`
  prices a candidate universe through `strategy_engine` (one shared leg
  cache per scan) and measures each resulting history through
  `range_analytics`, then offers separate, optional filtering
  (`apply_filters`/`FilterCriterion`) and transparent multi-key ranking
  (`rank_results`/`SortKey`) over the results — never a composite/opaque
  score, never a hard-coded "range-bound" threshold.
  - **Unavailable-market-data hardening**: a candidate whose leg is
    confirmed unavailable by LSEG (`MarketDataUnavailableError`) is
    skipped and recorded on `ScanReport.skipped`, and the scan continues
    — it does not abort. A RIC confirmed unavailable is remembered for
    the rest of that scan so later candidates referencing it are skipped
    without a repeat LSEG attempt. Every other exception (network/
    session/auth/vendor errors, programming bugs) still propagates and
    aborts the scan — this is deliberately narrow, not a general failure
    bucket.
  - **Canonical metric resolution**: `metric_value()` (`template_scanner/
    metrics.py`) is the single resolver for "a scalar metric by name on a
    RangeAnalytics" — either a direct dataclass field (e.g.
    `efficiency_ratio`) or a derived metric (`normalized_crossing_frequency`,
    `range_to_volatility_ratio`, `robust_to_full_width_ratio`).
    `results_to_dataframe()` and `filters.at_lookback()` both resolve
    through this one function, so a metric name means the same thing in
    the scanner result table and in filter/rank accessors.
- **Module 6A — Streamlit Range-Bound Scanner UI** (`ui/`): the scan
  configuration panel, strategy-template grid, ranked opportunity table,
  filters, and ranking controls. See [Current UI](#current-ui) below.
- **Module 6B — Selected-Strategy History Chart** (`ui/chart_view.py`):
  a Plotly chart of the selected candidate's historical Strategy series
  with its robust low/median/high levels overlaid, built entirely from
  data the scan already fetched/computed — selecting a row or switching
  the chart's horizon never re-downloads market data or re-runs analytics.
- **Module 7A — Strategy Set Engine** (`strategy_sets/`): named,
  JSON-persisted collections of strategy definitions (`StrategySet`)
  that expand into the existing `strategy_engine.StrategyInstance`
  architecture via `expand_strategy_set()`. `contract_start`/
  `contract_end` are call-time arguments, never persisted, matching
  `ScanRequest`'s own convention.
- **Module 7B — Strategy Set UI** (`ui/strategy_set_view.py` and
  friends): the Strategy Set selector, Save/"+ New"/Delete controls,
  and per-row Market/Interval grid columns, integrated directly into
  the existing Strategy Templates grid — a Strategy Set is a saved,
  named version of that one grid, not a second table or a second Run
  Scan button.
- **Module 8 — Strategy Set Import** (`strategy_import/`): imports a
  CSV file or an Excel workbook (one worksheet = one Strategy Set; one
  CSV file = one Strategy Set) into ordinary `StrategySet` objects, via
  an in-memory parse → validate → preview pipeline — nothing is written
  to disk until the user explicitly confirms Import. See [Strategy
  Sets, Import & Run-Time Scanning](#strategy-sets-import--run-time-scanning)
  below.
- **Module 9 — Strategy Set Scan** (`strategy_sets/execution.py`): runs
  an already-saved Strategy Set at one user-chosen interval, applied
  transiently to every entry for that run only — the saved Strategy
  Set is never modified. A second, additive execution path alongside
  Module 6A's grid-based Run Scan, sharing the same underlying
  `run_scan_on_instances()`.

## Supported intervals

- `DAILY`
- `HOURLY`
- `4H` (synthesized from `HOURLY` bars, not a native LSEG interval)

Lookback windows (see [Current UI](#current-ui)) are counted in
**observations/bars of the selected interval, not calendar days** — a
lookback of 60 on a `4H` scan spans a different amount of wall-clock time
than 60 on `DAILY`.

## Market RIC conventions & data-field differences

Each market's exact LSEG RIC convention (root + expiry-year digit count) is
declared entirely as data on `core.config.MarketDefinition` — `core.ric.
build_ric()` is the single, generic RIC builder and contains no per-market
branching:

| Market | RIC root | Year digits | Example (Sep-2026) |
|---|---|---|---|
| SOFR | `SRA` | 2 | `SRAU26` |
| FED_FUNDS | `FF` | 2 | `FFU26` |
| SONIA | `SON` | 1 | `SONU6` |
| CORRA | `CRA` | 1 | `CRAU6` |
| ESTR (€STR) | `SRE` | 2 | `SREU26` |

Live LSEG testing also found genuine field-population differences across
markets at the `DAILY` interval, handled entirely inside `core.downloader.
_normalize_columns` (never a per-market special case):

- **SOFR / Fed Funds / €STR** — `TRDPRC_1` (canonical `Close`) and `SETTLE`
  are both populated, and can differ slightly. `Close` is always derived
  from `TRDPRC_1`; `SETTLE` is never consulted for these markets.
- **SONIA** — `TRDPRC_1`/`OPEN_PRC`/`HIGH_1`/`LOW_1`/bid/ask are entirely NA
  at `DAILY`, but `SETTLE` is populated. `Close` row-wise falls back to
  `SETTLE` only where the primary source is missing (`Close =
  Close.fillna(SETTLE)`, never a global replacement), and only for `DAILY`
  requests — never `HOURLY`/`4H`, and Open/High/Low are never fabricated
  from `SETTLE`.
- **CORRA** — RIC construction is correct (`CRAU6`, `CRAH7`, ...), but the
  current LSEG account lacks entitlement for this universe
  (`TS.Interday.UserNotPermission.70112`, "User does not have
  permission for this universe") — a permissions issue, not an Oscill8
  bug. This exact, confirmed condition is now translated into the same
  typed `MarketDataUnavailableError` as the `70005` case above (`core/
  downloader.py::_is_confirmed_no_permission()`), so a scan containing
  CORRA alongside other markets skips the CORRA candidates and still
  returns results for the rest — it no longer aborts the whole scan.
  CORRA's entitlement gap is interval-independent (skipped identically
  at `DAILY`/`HOURLY`/`4H`). SONIA's own `HOURLY`/`4H` data
  availability is a separate, still-unverified question — see [Current
  limitations](#current-limitations--deferred-work).

Note the distinction between three separate vocabularies, kept
structurally separate in the codebase: a market's **LSEG RIC root**
(this table, e.g. `SRA`), Oscill8's **internal registry key**
(`core.config.MARKETS`'s dict keys, e.g. `"SOFR"`), and a **vendor/
workbook market code** used in an imported strategy file (e.g. `SRA`,
`SON`, `CRA`, `ER`, `YBA`, `FSR` — see [Strategy Sets, Import &
Run-Time Scanning](#strategy-sets-import--run-time-scanning) below).
The first two happen to coincide with the RIC root for some markets
here, but nothing in the codebase assumes they always will —
`strategy_import.market_mapping` is the one explicit bridge from the
third vocabulary to the second.

## Templates and candidate generation

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

**This is a position-relative, rolling-template scanner.** A template
defines a shape by *relative curve position* (offset 0, 1, 2, ...), and
the scanner rolls that shape across every eligible starting point in the
selected contract universe — "curve position 1" is therefore a different
real contract for each rolled candidate the scan produces, not one fixed
RIC. The scanner does **not** currently support an explicit "pick these
exact real contracts" mode, and does not support a continuous, contract-
independent "generic curve" price series either — see
[Current limitations](#current-limitations--deferred-work).

## Current UI

Run with `streamlit run ui/app.py` (see [Running the application](#running-the-application)).
The workflow, top to bottom:

1. **Scan configuration panel** — Market, Interval, contract Universe
   date range (which contracts get rolled into candidates), price
   History date range (what date range gets priced for those legs, an
   independent window from Universe), Lookbacks (bars) — one or more
   analysis horizons in bars/observations, not calendar days — and
   Primary Lookback: *"the horizon used for the headline range metrics
   shown for each candidate; other requested lookbacks are used for
   multi-lookback/stability analysis."* Run Scan triggers exactly one
   `run_scan()` call; nothing below it re-runs the scan.
2. **Strategy Templates grid** — one row per template, curve positions
   (bare numbers, see [Templates](#templates-and-candidate-generation)
   above for why they're not contract codes) as editable columns. `0` or
   a blank cell means "skip this position." Multiple rows/multiple
   templates in one scan are supported, as is adding/removing rows and
   changing how many position columns are shown.
3. **Range-Bound Opportunities** — after Run Scan: an analyzed/skipped/
   shown status line, a "Ranked by: `<metric>` ↑/↓ · Lower/Higher is
   better" label reflecting the current ranking, `Ranking ▾`/`Filters ▾`
   popovers (primary + optional secondary ranking key; the existing
   filter set — Efficiency Ratio, Normalized Crossing Frequency, AR(1)
   Beta, Half-Life, Robust Range Width, AR(1) R², and one Module 4B
   stability filter — each independently enable/disable-able, no
   threshold hard-coded), and the ranked result table itself (`Rank`,
   `Strategy`, `Ratio`, `Current`, `Median`, `Pos`, `Width`, `ER`, `Cross
   Freq`, `Half-Life`, `AR1 β`). Skipped candidates (unavailable RICs)
   stay visible in an expander, never silently hidden.
4. **Selected Strategy** — clicking a result row's checkbox selects it
   (the whole row highlights); a summary panel shows its rank, RICs,
   ratio, interval, and headline Current/Median/Robust Range/Position/ER
   at the Primary Lookback.
5. **Selected Strategy History chart** (Module 6B) — the selected
   candidate's Strategy price series plotted against its Robust Low/
   Median/Robust High levels, with a Chart Horizon selector limited to
   whichever lookbacks that scan actually requested. Both the initial
   render and switching Chart Horizon reuse data the scan already
   fetched/computed — no new LSEG call, no new SQLite fetch beyond a
   cache hit for the single already-scanned candidate, no analytics
   recomputation.

## Strategy Sets, Import & Run-Time Scanning

A **Strategy Set** (Module 7A) is a named, JSON-persisted collection of
strategy definitions — e.g. "Churning", "6M Strategies". Module 7B
integrates it directly into the Strategy Templates grid: the selector,
Save/"+ New"/Delete controls sit in that section's own header, and a
loaded Strategy Set becomes ordinary grid rows, run through the exact
same Run Scan button and `run_scan()` call a manually-typed row uses —
there is no second table, no second Run Scan button, and no separate
Strategy-Set-specific scan path for the grid.

### Import (CSV/XLSX)

Module 8 imports a Strategy Set from an external file:

- **One CSV file = one Strategy Set.** **One Excel worksheet = one
  Strategy Set** — a multi-sheet workbook produces multiple Strategy
  Sets in one import.
- **Nothing is written until the user explicitly confirms Import.**
  Uploading a file only builds an in-memory preview (parse → validate →
  preview); `StrategySetRepository` is only ever written to from the
  one explicit "Import All" confirmation.
- **Every row is classified three ways**, never silently dropped:
  **ready** (a valid, importable strategy), **unavailable** (a
  recognized market with no data-provider configuration — see below),
  or **invalid** (an unrecognized market code, a non-numeric position
  value, or another structurally malformed row) — shown with its row
  number, label, and exact reason.
- **A trader's Label is not a unique identifier.** Strategy identity for
  deduplication is the resulting `StrategyDefinition` (market + offsets
  + weights), never the Label — the same Label legitimately recurs
  across markets and, within one market, across genuinely different
  position structures. A blank position cell and an explicit `0` are
  treated identically.
- **Duplicate Strategy Set names are never overwritten** — re-importing
  produces `"Name"`, `"Name 2"`, `"Name 3"`, ... rather than silently
  replacing an existing set.
- **Market codes** used in an imported file (short, RIC-root-*style*
  codes, not necessarily identical to the market's actual LSEG RIC
  root or Oscill8's internal registry key — see [Market RIC
  conventions](#market-ric-conventions--data-field-differences) above):

  | Workbook code | Resolves to | Status |
  |---|---|---|
  | `SRA` | `SOFR` | Ready — configured, scannable via LSEG |
  | `SON` | `SONIA` | Ready — configured, scannable via LSEG |
  | `CRA` | `CORRA` | Ready — configured; LSEG entitlement currently missing (see above), skipped per-scan, not per-import |
  | `ER` | Euribor | Recognized, **not** configured — no `MarketDefinition` exists |
  | `YBA` | an Australian exchange market | Recognized, **not** configured — no `MarketDefinition` exists |
  | `FSR` | SARON 3M futures | Recognized, **not** configured — no `MarketDefinition` exists |

  `ER`/`YBA`/`FSR` rows are never dropped or misreported as errors —
  they're classified **unavailable**, with their own specific reason,
  and simply never persisted. Any other code is **invalid**
  (unrecognized).

### Strategy Set Scan (run-time interval selection)

Module 9 adds a second, additive way to run a saved Strategy Set: pick
the set, pick ONE interval, and that interval is applied to every
entry in the set **for that run only** — a transient, in-memory copy,
never written back to the saved file. The same saved Strategy Set can
be run at a different interval on a later occasion without any
modification or re-import. This does not change the grid's own
per-row Market/Interval behavior in any way.

## Range-Bound Metrics

Every metric below is read directly from `range_analytics` source — not
guessed. All are computed over a selected window (a lookback of the last
N valid observations, or a calendar `start`/`end`, per `analyze_range()`
in `range_analytics/results.py`).

- **`mean` / `median`** — arithmetic mean and median of the Strategy
  series over the window (`location.py`).
- **`range_low_full` / `range_high_full` / `range_width_full`** — the
  window's plain min/max/width.
- **`range_low_robust` / `range_high_robust` / `range_width_robust`** —
  the window's **5th and 95th percentiles** (`series.quantile(0.05)`,
  `series.quantile(0.95)`) and their difference. **These percentile
  bounds (5/95) are currently hard-coded** — there is no configurable
  percentile band today (see [Current limitations](#current-limitations--deferred-work)).
- **`range_position_full` / `range_position_robust`** — `(current - low)
  / (high - low)` against the full or robust range respectively.
  Deliberately **not clipped to [0, 1]** — a value outside that band
  (current sits below the historical low, or above the P95) is itself
  meaningful and shown as such (e.g. as a percentage that can exceed
  100% or go negative in the UI).
- **`realized_vol_price` / `realized_vol_bp`** — sample standard
  deviation (`ddof=1`) of period-over-period level changes
  (`volatility.py`); not annualized, not a percentage-return
  calculation. `realized_vol_bp` converts via each market's own
  `bp_per_point` (`units.py`), never a hard-coded ×100.
- **`efficiency_ratio`** — Kaufman-style: `abs(S_T - S_0) / sum(abs(ΔS_t))`
  (`efficiency.py`). Near 0 means a large amount of back-and-forth
  movement relative to net displacement (potentially range-bound); near
  1 means movement was predominantly directional (potentially trending).
  Cannot by itself distinguish genuine oscillation from a flat/illiquid
  series — intended to be read alongside range width and realized
  volatility.
- **`raw_crossing_count` / `hysteresis_crossing_count`** — number of
  confirmed directional crossings of an equilibrium level (the window's
  median by default), via `oscillation.count_crossings()`. `raw` always
  uses a zero-width band; `hysteresis` uses the scan's configured
  `crossing_threshold` (default 0.0, i.e. identical to raw) to avoid
  tick-level noise inflating the count.
- **`normalized_crossing_frequency`** — a Module 5B derived metric,
  `hysteresis_crossing_count / (observation_count - 1)`
  (`template_scanner/metrics.py`), NaN when fewer than 2 observations.
- **`range_to_volatility_ratio`** — a Module 4B/5B derived metric: the
  robust range width (in bp) divided by realized volatility (in bp) —
  how large the historical range is relative to a typical single-bar
  move. Does not by itself indicate oscillation (a slow steady trend
  across a wide span produces the same ratio as a wide oscillating
  range) — intended to be combined with efficiency ratio, crossing
  frequency, and AR(1) beta.
- **`robust_to_full_width_ratio`** — `range_width_robust /
  range_width_full`: near 1 means the robust and full ranges agree; much
  less than 1 means the full range is dominated by a few outlier prints.
- **`ar1_beta` / `ar1_gamma` / `ar1_std_error` / `ar1_r_squared`** — an
  AR(1) fit on level changes, `ΔS_t = α + γ·S_(t-1) + ε_t`
  (`mean_reversion.py`); `beta = 1 + γ` is reported because its sign/
  magnitude directly answers smooth-reversion (`0 < beta < 1`) vs.
  oscillatory-reversion (`-1 < beta < 0`) vs. random-walk (`beta == 1`)
  vs. non-mean-reverting (`|beta| >= 1`), without requiring the reader to
  remember the `+1` shift.
- **`half_life`** — `ln(2) / (-ln(|beta|))` for `0 < |beta| < 1`; `0.0`
  at `beta == 0` (instant reversion); `NaN` when `|beta| >= 1`
  (random-walk or non-mean-reverting — no finite half-life exists).
- **Multi-lookback stability statistics** (Module 4B, `stability.py` /
  `multi_lookback.py`) — for `range_width_robust`, `range_low_robust`,
  `range_high_robust`, `median`, `realized_vol_bp`, `efficiency_ratio`,
  `normalized_crossing_frequency`, `ar1_beta`, `half_life`, and
  `range_to_volatility_ratio`: each metric's own value at every requested
  lookback, plus `stdev`, `min`/`max`, `short_vs_long_diff` (and
  `short_vs_long_ratio` for metrics where a ratio is meaningful —
  "signed" metrics like `ar1_beta` never populate it), and pairwise
  diffs/ratios between adjacent lookbacks. Purely descriptive — no
  stability verdict or score is computed.

None of the above is recomputed in `ui/` — every value the UI displays
(result table, Selected Strategy panel, chart overlay) is read directly
from an already-computed `RangeAnalytics`/`MultiLookbackAnalytics`
object.

## Current limitations / deferred work

- **Configurable robust-range percentile bounds** — not implemented. The
  5th/95th percentile robust-range bounds are currently hard-coded (see
  [Range-Bound Metrics](#range-bound-metrics)); letting a user choose a
  different band (e.g. 10/90, 25/75) is a considered future change, not
  yet built and not yet approved.
- **Z-score / current-dislocation analytics** — not implemented. A
  standard and/or robust Z-score to separate "quality of historical
  range-boundedness" from "current distance from equilibrium" is under
  consideration; its exact statistical definition has not been approved.
- **Intermarket strategies** (legs spanning more than one market) — not
  implemented. `StrategyDefinition.market_key` is singular by design;
  cross-market alignment/risk-normalization is deferred.
- **Explicit "Real Contract" mode** (scanning one specific, user-picked
  set of dated contracts rather than a rolled template) — not
  implemented in the UI. The backend primitives it would need
  (`StrategyInstance`, `build_history()`, `template_scanner.
  analyze_histories()`, `core.futures_calendar.generate_contracts()`)
  already exist, but `run_scan()` does not currently expose an
  instances-in/`ScanReport`-out entry point with the skip-handling that
  a UI for this would need without duplicating scanner.py's internal
  loop.
- **True Generic-vs-Real-contract mode distinction** — not implemented.
  Today's scanner is a single position-relative rolling-template mode
  (see [Templates](#templates-and-candidate-generation)); it is not a
  continuous contract-independent "generic curve" series, and should not
  be described as one.
- **Composite Range Score** — not implemented and not planned without a
  separate design/validation pass. Filtering and ranking are always
  transparent, single-metric operations (`FilterCriterion`, `SortKey`) —
  never a blended/opaque score.
- **Secondary diagnostic charts** (AR(1) fit visualization, crossing
  markers, rolling-stability chart, z-score chart) — deferred. Module 6B
  ships exactly one primary strategy-history chart.
- **Saved scans / export workflow** — not implemented; there is no
  persistence of a `ScanRequest`/`ScanReport` beyond the current browser
  session's `st.session_state`. A Strategy Set (Module 7A) is a named
  collection of strategy definitions only — it does not capture a
  price window, lookbacks, results, or (Module 9) the run-time interval
  a scan was run at.
- **Full `MarketDefinition` configuration for Euribor, YBA (an
  Australian exchange market), and SARON 3M futures (`FSR`)** — not
  implemented. `strategy_import.market_mapping` recognizes all three
  by name and their RIC roots are confirmed, but none has the
  mandatory `exchange`/`bp_per_point` metadata, which must never be
  guessed (see [Strategy Sets, Import & Run-Time Scanning](#strategy-sets-import--run-time-scanning)
  above). **None of the three is currently tradable/scannable through
  LSEG or any other data provider.**
- **SONIA `HOURLY`/`4H` data availability** — unverified. SONIA's
  `DAILY` behavior is confirmed working live; no repository evidence
  exists for what LSEG returns for SONIA at intraday intervals, and
  nothing has been implemented for it. Do not assume it behaves like
  CORRA's `70112` entitlement case above — that requires a live LSEG
  call to confirm before any code is written.
- **QuantHub (QH) as a secondary/fallback data provider** — mentioned
  as a future direction for markets whose LSEG entitlement is
  currently missing (e.g. CORRA), but **no QH code, integration, or
  design exists anywhere in this repository today.**

## Running the application

```
pip install -r requirements.txt
streamlit run ui/app.py
```

The UI needs LSEG Workspace running and an authenticated `lseg.data`
session only when a scan actually needs to fetch data not already cached
in SQLite (`data/oscill8.db`) — cache-hit scans and any interaction with
an already-selected candidate's chart never touch LSEG (see
[Current UI](#current-ui)). `core.config.LSEG_SESSION_TYPE` defaults to
`"desktop.workspace"`.

## Testing

```
pytest -q
```

Current suite: **879 passed, 1 skipped** (unit tests, LSEG fully mocked
— no live session required; this is a snapshot as of the Strategy Set
Scan / Module 8-9 / CORRA-classification work described above — re-run
the command above for the up-to-date count, do not trust this number
blindly). Verified directly (`pytest -q -rs`): the 1 skip is `tests/
test_ui_keyboard_browser.py`, a real-browser keyboard-workflow check
that skips when Playwright/Chromium isn't available in the current
environment (it wasn't in the environment this count was verified
in) — unrelated to LSEG; see below for `test_live_connection.py`.
`tests/test_cache.py::test_read_bars_output_matches_downloader_canonical_schema`
may fail in environments with pandas >= 3.0 (asserts `datetime64[ns]`;
newer pandas defaults to `datetime64[us]`) — pre-existing, unrelated to
any Strategy Set/import work, not fixed here; passes with the pinned
`requirements.txt` pandas version.

`test_live_connection.py` is a manual smoke test, not part of the pytest
suite — run it directly (`python test_live_connection.py`) on a machine
with LSEG Workspace open. Only the `SOFR` market is currently marked
`verified=True` in `core/config.py`; SONIA/CORRA/ESTR/FED_FUNDS RIC roots
have since been confirmed via live LSEG data pulls (see [Market RIC
conventions](#market-ric-conventions--data-field-differences) above), but
`verified=True` is reserved specifically for a live chain/search
confirmation and has not been flipped for them.

## Repository structure

```
core/              LSEG downloader, RIC build/parse, futures calendar, market config
database/          SQLite cache (get_history) sitting between core and everything above it
strategy_engine/   StrategyDefinition, rolling contract combinations, historical pricing
range_analytics/   Range-bound (4A) and multi-lookback stability (4B) measurements
template_scanner/  Dense-grid templates, candidate universe, scan orchestration, filtering/ranking (5A/5B)
strategy_sets/     Strategy Set domain model, JSON persistence, expansion (7A), run-time
                   interval-override execution (9) -- execution.py, expansion.py, model.py,
                   repository.py, serialization.py
strategy_import/   CSV/XLSX Strategy Set import: parse -> validate -> preview -> commit (8)
ui/                Streamlit UI (6A/6B/7B/8/9) -- app.py, state.py, controls.py, scan_view.py,
                   results_view.py, chart_view.py, formatting.py, strategy_set_view.py,
                   strategy_set_scan_view.py, strategy_import_view.py
tests/             Unit tests for every module above (pytest, LSEG mocked)
```

## Current status

Modules 1 through 9 (LSEG data layer through Strategy Set import and
run-time scanning) are complete and tested, along with the CORRA
entitlement-error classification fix. See [Current limitations /
deferred work](#current-limitations--deferred-work) for what is
explicitly out of scope today — in particular, Euribor/YBA/SARON have
no configured `MarketDefinition` and are not scannable through any
data provider, SONIA's `HOURLY`/`4H` availability is unverified, and
QuantHub (QH) integration does not exist.
