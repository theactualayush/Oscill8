# Oscill8 — Range-Bound Strategy Scanner

Oscill8 is an internal quantitative research application for discovering
range-bound relative-value opportunities across global interest-rate
futures markets (SOFR, Fed Funds, SONIA, CORRA, Eurozone STIR / €STR,
EURIBOR, SARON, and the Australian 90-Day Bank Bill).

The system generates multi-leg strategy candidates (outrights, spreads,
flies, condors, and arbitrary custom weight/offset shapes), builds their
historical price series, measures range-bound behaviour, and lets a
trader filter and rank candidates through a compact scanner grid before
drilling into a chosen strategy's own historical chart.

## Architecture

```
LSEG Workspace      QuantHub API
      ↓                   ↓
       \_________________/
                ↓
      Provider Layer (core/downloader.py, core/quanthub.py, core/providers.py)
                ↓
      SQLite Cache + Provider Provenance   (database/)
                ↓
      Strategy Engine                       (strategy_engine/)
                ↓
      Range-Bound Analytics                  (range_analytics/)
                ↓
      Template / Scanner Engine               (template_scanner/)
                ↓
      Streamlit UI                             (ui/)
```

Only the data/downloader layer (`core/`) talks to LSEG or QuantHub — a
per-`(ric, interval)` decision of which of the two actually serves a
given contract/interval is made once and persisted in SQLite (see
[Data providers: LSEG and QuantHub](#data-providers-lseg-and-quanthub)
below). Every layer above `database/` operates on normalized Pandas
DataFrames and has no LSEG or QuantHub dependency — enforced by tests,
not just convention. `strategy_engine/`, `template_scanner/`, and
`range_analytics/` only ever call `database.get_history`/
`get_history_batch` and are unaware a second provider exists at all. The
UI layer is intentionally thin: it never computes analytics, never
duplicates filtering/ranking/derived-metric formulas, and never talks to
LSEG or QuantHub directly — it only translates user input into calls
against `template_scanner`'s public API and formats that API's output
for display.

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
- **Module 7A — Strategy Set Engine** (`strategy_sets/`): user-owned,
  named, JSON-serializable collections of `StrategyDefinition`s ("Strategy
  Sets" — e.g. "Churning", "6M Strategies"), with `expand_strategy_set()`
  rolling an enabled set into the same `StrategyInstance[]` shape
  `template_scanner` already builds internally. The contract-selection
  window is a call-time argument, not part of the saved object, so a
  saved set never goes stale as "today" moves. (Additional Strategy Set
  work beyond this engine has since landed in the repository but is out
  of scope for this documentation pass — see the note at the end of this
  README.)
- **Module 8 — QuantHub Secondary Provider & Provider Provenance**
  (`core/quanthub.py`, `core/providers.py`, `database/`): a second market-
  data provider for markets LSEG cannot fully serve, with a persisted
  per-`(ric, interval)` provider decision and a fix that excludes the
  currently-forming bar from every fetch/cache/return path. See
  [Data providers: LSEG and QuantHub](#data-providers-lseg-and-quanthub)
  below.
- **Module 9 — Intermarket Strategy Engine** (`strategy_engine/
  intermarket_definitions.py`, `intermarket_combinations.py`;
  `strategy_sets/` additions): lets a SINGLE strategy combine legs from
  DIFFERENT markets (e.g. a SOFR leg + a SONIA leg priced into one
  series) as an additive sibling to the existing single-market
  `StrategyDefinition`/`StrategyInstance` path, which is unmodified.
  `IntermarketDefinition`/`LegSpec` model each leg's own market/offset/
  weight; `generate_intermarket_instances()` intersects only the
  `offset=0` "anchor" legs' calendars to find valid anchor periods, then
  steps each other leg forward on ITS OWN market's contract curve.
  Wired into `strategy_sets` (`IntermarketStrategySetEntry`,
  `StrategySet.intermarket_entries`, JSON-authorable) and into
  `template_scanner`/`range_analytics` for pricing, filtering, ranking,
  and range/volatility diagnostics (bp-conversion is explicit-or-`NaN`,
  never guessed from one leg's market). **Backend only** — there is no
  Streamlit UI for authoring/editing intermarket entries (hand-edit the
  JSON), and no live UI button yet triggers the Strategy Set execution
  path (`strategy_sets/execution.py`) for either single-market or
  intermarket entries.

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

| Market | RIC root | Year digits | Example (Sep-2026) | LSEG `verified` |
|---|---|---|---|---|
| SOFR | `SRA` | 2 | `SRAU26` | `True` |
| FED_FUNDS | `FF` | 2 | `FFU26` | `False` |
| SONIA | `SON` | 1 | `SONU6` | `False` |
| CORRA | `CRA` | 1 | `CRAU6` | `False` |
| ESTR (€STR, CME) | `SRE` | 2 | `SREU26` | `False` |
| EURIBOR | `FEI` | 1 | `FEIU6` | `False` |
| SARON | `SARO3` | 1 | `SARO3U6` | `False` |
| YBA (AU 90-Day Bank Bill) | `YBA` | 1 | `YBAU6` | `False` |
| ESTR_ICE (€STR, ICE Europe) | `EON3` | 1 | `EON3U6` | `False` |

`verified=True` is reserved specifically for a live LSEG chain/search
confirmation in `core/config.py`; only SOFR currently carries it. CORRA,
SONIA, EURIBOR, SARON, YBA, and ESTR_ICE are all routed to QuantHub as a
fallback provider (see
[Data providers: LSEG and QuantHub](#data-providers-lseg-and-quanthub)
below) — LSEG is still tried first the first time each `(ric, interval)`
is requested, and QuantHub only takes over permanently for that
`(ric, interval)` if LSEG's first attempt is incomplete/unavailable —
so a scan still gets usable data for these markets even where LSEG
entitlement is currently missing (CORRA) or simply unverified
(EURIBOR/SARON/YBA/ESTR_ICE).

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
  (`TS.Interday.UserNotPermission.70112`) — a permissions issue, not an
  Oscill8 bug.

## Data providers: LSEG and QuantHub

`database.get_history`/`get_history_batch` (`database/service.py`) are
still the only public entry points — callers never learn or choose which
provider actually served a bar. Internally, `core.providers.
resolve_provider(market_key)` looks up `PROVIDER_ROUTING`
(`core/providers.py`) to decide which provider is *eligible* to serve a
market: absence from that dict means LSEG only; presence (today: CORRA,
SONIA, EURIBOR, SARON, YBA, ESTR_ICE) means QuantHub is available as a
fallback. A market being QuantHub-eligible does **not** mean QuantHub is
used automatically — the actual choice is a one-time, persisted decision
per `(ric, interval)`, described below.

### Why the SQLite cache matters more for QuantHub than for LSEG

QuantHub's HTTP API was live-tested and confirmed to support only three
effective parameters: `instruments=`, `interval=`, and `count=`.
`start=`/`end=` return HTTP 500; `from=`/`to=`/`offset=`/`page=`/
`cursor=`/`before=` are all silently ignored. There is **no way to ask
QuantHub for an arbitrary historical date range** — `count=N` always
means "the most recent N observations as of right now," with no anchor
to an earlier reference point. There is also a hard per-request ceiling,
`QUANTHUB_MAX_ROWS_PER_REQUEST = 10_000` (10,000 rows succeeds, 10,001
returns HTTP 400), and a `QUANTHUB_BATCH_SIZE = 10` cap on distinct
instruments per request (`core/quanthub.py`;
`_max_count_for_batch(batch_size) = 10_000 // batch_size` — e.g. 10
instruments in one request caps each to 1,000 rows).

This is why the SQLite cache is load-bearing for a QuantHub-served
`(ric, interval)`, not just a performance optimization: once a bar has
been fetched and persisted, it never needs to be re-requested from
QuantHub, because QuantHub itself cannot be asked for "just the new
part" — every subsequent QuantHub fetch for that `(ric, interval)` is a
**full-window re-request of the most recent N observations**, not an
incremental one (see "Established QUANTHUB" in the state machine below).
A cold-start `(ric, interval)` that has never been cached pays this
full-window cost once; every following scan against the same
`(ric, interval)` is a pure cache read for any window inside what's
already stored, and pays the full-window QuantHub cost again only when
the requested window extends beyond what's cached.

### Provider provenance: the state machine

A nullable `provider` column on `database.models.SyncRange` (`"LSEG"` /
`"QUANTHUB"` / `NULL`), keyed on `(ric, interval)` together (the same
contract can be established LSEG at `DAILY` and established QuantHub at
`HOURLY`), records which provider actually serves each QuantHub-eligible
`(ric, interval)` — decided once, then permanent until an operator
explicitly resets it (`database.cache.delete_bars_and_sync_ranges()`).

**Genuinely new `(ric, interval)`** — nothing cached yet:

```
New (ric, interval) request
        |
   Try LSEG for the FULL requested window
        |
   Is the response COMPLETE?
   (non-empty, no interior gap wider than a
    generous business-day threshold)
        |
        |-- Yes --> ESTABLISH provider=LSEG
        |           persist LSEG bars
        |
        \-- No ---> discard the LSEG attempt (never persisted)
                    ESTABLISH provider=QUANTHUB
                    fetch + persist QuantHub for the full window
```

This completeness test runs **exactly once** per `(ric, interval)` —
never repeated. Once established:
- **Established LSEG** — every later request does the original,
  unchanged incremental fetch: only the missing sub-range(s) from LSEG.
  QuantHub is never consulted again for this `(ric, interval)`.
- **Established QUANTHUB** — whenever anything is missing, QuantHub has
  no way to fetch "just the gap," so the full requested window is
  re-requested from QuantHub every time. LSEG is never consulted again
  for this `(ric, interval)` either way.

**Legacy/unknown `(ric, interval)`** — `provider` is `NULL` but
`sync_ranges` coverage already exists (e.g. cached before the provider
column existed):

```
Legacy/unknown (ric, interval), coverage exists, provider=NULL
        |
   Per missing sub-range:
        |
   Try LSEG first
        |
        |-- Complete/usable --> persist that sub-range, provider stays NULL
        |
        \-- Unavailable, incomplete, or empty
              |
              --> fall back to QuantHub for JUST that sub-range
                  persist that sub-range, provider STILL stays NULL
```

`provider` stays `NULL` on every branch here regardless of which
provider actually served the data — the pre-existing history's true
origin is unknown and must never be fabricated as either provider. This
is the **one deliberate exception** to "a `(ric, interval)`'s history is
never a mix of LSEG and QuantHub bars": a legacy row can accumulate
sub-ranges from both providers over time, forever, since it never
transitions to an established state. The only way to move a legacy row
into LSEG/QuantHub establishment is a full reset via
`cache.delete_bars_and_sync_ranges()` followed by a fresh request.

### How LSEG responses are classified

`core/downloader.py` translates specific, confirmed LSEG error
conditions into a typed `MarketDataUnavailableError` — narrow, exact
classifiers, never a broad catch-all, and excluded from the module's
retry logic (a genuine outage/auth/network error still retries):

| Outcome | Result |
|---|---|
| Valid data returned | Used as-is |
| Empty response (valid RIC, no bars in range) | Returned as empty, not an exception — flows into the same "incomplete" branch as a confirmed-unavailable error during establishment/legacy fallback |
| `TS.Interday.UserRequestError.70005` ("The universe is not found") | `MarketDataUnavailableError` — confirmed invalid RIC |
| `TS.Interday.UserNotPermission.70112` | `MarketDataUnavailableError` — confirmed missing Interday entitlement (CORRA's known LSEG account limitation) |
| `*.UserNotPermission.92000` (any service/product prefix) | `MarketDataUnavailableError` — confirmed missing Intraday entitlement, matched on **error code alone**, prefix-agnostic: live-confirmed in production as both `TS.Intraday.UserNotPermission.92000` and `TSCC.QS.UserNotPermission.92000`. The English wording after the code has also been observed to vary — `"User does not have permission for this universe"` vs. the real production wording `"User has no permission"` — so only the `UserNotPermission.92000` code substring is authoritative for this classifier, never a specific prefix or trailing phrase |
| Any other LSEG error | Not caught here — propagates and aborts the caller (network/session/auth/vendor errors, programming bugs) |

### Currently-forming bars are never fetched, cached, or returned

Every request's `end` is capped, before any cache-coverage check or
provider call, to the last **fully closed** bar as of "now"
(`_effective_request_end`, `database/service.py`) — bars are
open-labeled, so a `4H` bar dated 12:00 spans `[12:00, 16:00)` and is
still forming until 16:00. This fixes a real production bug: a plain
date-range request used to stay uncapped to day-end, so a still-forming
interval was re-requested from the provider on every identical re-scan,
since nothing in that always-in-the-future tail could ever be marked
synced. A repeat scan during the same still-forming period now makes
zero further provider requests for that bar.

### No duplicate fetches across a scan's shared legs

`strategy_engine.pricing.prewarm_leg_cache()` batches every distinct leg
a scan needs through `database.get_history_batch()` once, up front, into
a shared `LegCache` used by every candidate — a RIC shared across
several rolled candidates (e.g. adjacent flies sharing two of three
legs) is fetched from `database.get_history`/QuantHub at most once per
scan, regardless of how many candidates reference it.

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
   stay visible in an expander, never silently hidden. A "Columns ▾"
   popover (in-progress, uncommitted at the time of this documentation
   pass) additionally lets the trader show/hide optional result-grid
   columns via a multiselect — `Rank` and `Strategy` always stay visible
   since they identify the row; hiding a column is display-only and
   never removes the underlying metric from the scan result.
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
  session's `st.session_state`. Distinct from Module 7A's Strategy
  Sets, which persist a named collection of strategy *definitions* only
  — never a price window, lookbacks, or results.
- **LSEG live verification for EURIBOR/SARON/YBA/ESTR_ICE** — all four
  have complete, trader-confirmed `MarketDefinition`s and are served via
  the QuantHub fallback today, but none has been live-LSEG-tested in
  this environment; `verified` stays `False` for all four until that
  happens. CORRA's LSEG entitlement gap (`70112`) is a confirmed account
  permissions issue, not an RIC or code bug.
- **No narrower legacy-provenance migration** — a LEGACY/UNKNOWN
  `(ric, interval)` row (see
  [Data providers: LSEG and QuantHub](#data-providers-lseg-and-quanthub))
  can only be moved to an established LSEG/QuantHub state via a full
  reset (`database.cache.delete_bars_and_sync_ranges()`) followed by a
  fresh request — there is no "just tell me you're actually LSEG"
  operator command.

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

`ui/app.py` calls `load_dotenv()` before importing anything that
transitively imports `core.config`, so an `RBS_*` setting (e.g.
`RBS_QUANTHUB_TOKEN`) placed in a `.env` file at the repository root is
picked up automatically — no need to export it as a real OS/session
environment variable first. `python-dotenv` is already a pinned
`requirements.txt` dependency.

## Testing

```
pytest -q
```

Current suite (snapshot as of this documentation pass — re-run the
command above for the up-to-date count, do not trust this number
blindly): **1270 tests** — 1267 passing, 1 known environment-specific
failure (see below), 2 skipped. Unit tests, LSEG and QuantHub both
mocked — no live session required for the pytest suite itself.
`tests/test_cache.py::test_read_bars_output_matches_downloader_canonical_schema`
fails in environments with pandas >= 3.0 (asserts `datetime64[ns]`; newer
pandas defaults to `datetime64[us]`) — pre-existing, unrelated to any
market-data change, not fixed here. The 2 skips are
`tests/test_ui_keyboard_browser.py` (no playwright installed) and
`tests/test_quanthub_live_smoke.py` (`RBS_QUANTHUB_TOKEN` not set).

`test_live_connection.py` is a manual smoke test, not part of the pytest
suite — run it directly (`python test_live_connection.py`) on a machine
with LSEG Workspace open. `test_intermarket.py` (repository root) is a
second, similarly standalone/temporary script — it independently
verifies an intermarket Strategy Set's leg-RIC mapping and pricing
arithmetic against real LSEG/QuantHub data (see Module 9 above); run it
directly (`python test_intermarket.py`), not via pytest. Only the `SOFR` market is currently marked
`verified=True` in `core/config.py`; SONIA/CORRA/ESTR/FED_FUNDS RIC roots
have since been confirmed via live LSEG data pulls (see [Market RIC
conventions](#market-ric-conventions--data-field-differences) above), but
`verified=True` is reserved specifically for a live chain/search
confirmation and has not been flipped for them.

## Repository structure

```
core/              LSEG + QuantHub downloaders, RIC build/parse, futures calendar, market config,
                   provider routing (providers.py), market-instrument mapping table
database/          SQLite cache (get_history/get_history_batch) + provider provenance,
                   sitting between core and everything above it
strategy_engine/   StrategyDefinition, rolling contract combinations, historical pricing;
                   intermarket_definitions.py/intermarket_combinations.py (Module 9,
                   cross-market legs within one strategy)
range_analytics/   Range-bound (4A) and multi-lookback stability (4B) measurements
template_scanner/  Dense-grid templates, candidate universe, scan orchestration, filtering/ranking (5A/5B)
strategy_sets/     Named, JSON-persisted collections of StrategyDefinitions (7A)
ui/                Streamlit UI (6A/6B) -- app.py, state.py, controls.py, scan_view.py,
                   results_view.py, chart_view.py, formatting.py
tests/             Unit tests for every module above (pytest, LSEG and QuantHub mocked)
```

## Current status

Modules 1 through 9 (LSEG data layer through the Streamlit scanner UI,
selected-strategy history chart, Strategy Set engine, the QuantHub
secondary provider / provider-provenance / effective-request-end work,
and the intermarket strategy engine) are complete and tested at the
backend level. Module 9 (intermarket) has no Streamlit UI surface yet —
see its entry above. See
[Current limitations / deferred work](#current-limitations--deferred-work)
for what is explicitly out of scope today.

**Documentation scope note:** this README was last brought up to date
specifically for the QuantHub/provider-provenance work (Module 8) and
the market-metadata additions it depends on. A `git log` review during
that pass surfaced several further merged commits — a Strategy Set UI
panel wired into the scanner, configurable range-percentile bounds and
Z-score/absolute-Z-score analytics, movement/oscillation tradability
metrics, multi-market Strategy Set regression coverage, and a UI
redesign — that are **not yet reflected in this document**. They were
deliberately left out of this pass (scoped to QuantHub/provider work
only, per explicit instruction) rather than documented speculatively.
Treat any statement above about Module 7A/6A-6B scope, the default
result-table columns, or the scanner's filter/rank options as
potentially superseded by that later work until a follow-up
documentation pass covers it.
