# Oscill8 — Range-Bound Strategy Scanner

Oscill8 is an internal quantitative research application for discovering
range-bound relative-value opportunities across global interest-rate
futures markets: **SOFR, Fed Funds, SONIA, CORRA, and Eurozone STIR /
€STR**.

It generates multi-leg strategy candidates (outrights, spreads, flies,
condors, and arbitrary custom weight/offset shapes), builds their
historical price series, measures range-bound behaviour, and lets a
trader filter, rank, and inspect candidates through a dark, compact
scanner UI before drilling into a chosen strategy's own historical
chart.

## What it does

1. **Define a strategy shape** as a row of curve-position weights (e.g.
   `1 -2 1` for a fly) in the Strategy Templates grid — no need to name
   real contracts; Oscill8 rolls the shape across the currently active
   contract curve for you.
2. **Run a scan.** Oscill8 fetches historical prices for every rolled
   candidate (via a local SQLite cache backed by LSEG), builds each
   candidate's combined strategy price series, and measures range/
   location, movement, oscillation, and mean-reversion behaviour over
   one or more lookback windows.
3. **Filter and rank** the results transparently — by any single metric,
   never a blended/opaque score — and drill into a selected candidate's
   own historical chart with its robust range overlaid.

## Strategy Sets & the Strategy Workspace

The **Strategy Workspace** (top of the app) is where you build and save
strategies. There is exactly **one** working grid — Strategy Templates —
and a Strategy Set is simply a saved, named snapshot of that grid's
rows, not a separate editor:

- Each row has its own **Label**, **Market**, and **Interval** — a
  single Strategy Set can freely mix markets (e.g. SOFR + SONIA + CORRA
  in one saved set), and each row rolls independently.
- **Universe** (which listed contracts are eligible) is automatic: from
  today out to a fixed forward horizon — there's no manual date range
  to configure. **History** (how much price data feeds the analytics)
  stays separately editable, defaulting to the last ~6 months.
- **Save / + New / Delete** live next to the Strategy Set selector.
  Deleting always asks for confirmation first — nothing is removed
  without an explicit second click.
- **Keyboard workflow**: click a cell once, then type a value and press
  **Tab** to commit it and move to the next cell — a full row (Label,
  Market, Interval, every weight column) can be entered without
  touching the mouse again. Press **Enter** only on a row's last cell
  to commit it and drop to the next row.

A loaded Strategy Set becomes ordinary grid rows and runs through the
exact same **Run Scan** button as a manually-typed row — there is no
separate "Strategy Set scan."

## How scanning works

1. **Strategy Templates** (top) define the shapes to scan; **Scan
   Configuration** (below it) sets Lookbacks and the robust-range
   Lower/Upper percentile band (defaults to the 5th/95th percentile,
   both configurable).
2. Pressing **▶ Run Scan** prices every candidate once and measures it
   across every requested lookback — nothing below Run Scan re-triggers
   a scan.
3. **Range-Bound Opportunities** shows the ranked result table
   (Strategy, Ratio, Current, Low/Median/High, Position, Z-score,
   Movement, Oscillation count, Efficiency Ratio, Half-Life), with
   `Ranking ▾` / `Filters ▾` popovers for transparent, single-metric
   sorting and filtering — never a composite score. Candidates skipped
   because a leg's market data was confirmed unavailable stay visible in
   an expander, never silently hidden.
4. Selecting a row shows a **Selected Strategy** summary and its
   historical chart (robust Low/Median/High overlaid), reusing data the
   scan already fetched — no extra market-data or LSEG calls.

A failed scan shows a short, plain-language message first (e.g. "market
data not available with the current access"); the full technical detail
is available in a collapsed "Technical details" section, never shown as
the primary error.

## Data dependency: LSEG

Oscill8's historical market data currently comes from **LSEG Workspace**
(the LSEG Data Library), through an authenticated LSEG Workspace desktop
session running locally. All requests go through a local SQLite cache
first — a scan only calls LSEG for date ranges not already cached, and
an already-selected candidate's chart never calls LSEG at all.

LSEG is the only data provider implemented today. A secondary provider
(Quanthub) is under investigation to cover markets/intervals LSEG
currently can't serve (see **Planned: Quanthub provider** below) — it
is **not implemented**, and using it requires no action from you today.

## UI structure

The app (`streamlit run ui/app.py`) is a single page, top to bottom:

1. **Strategy Workspace** — Strategy Set selector (Save/+New/Delete) +
   the Strategy Templates grid.
2. **Scan Configuration** — Market/Interval defaults for new rows,
   Contracts (automatic), Price History range, Lookbacks, percentile
   band, and the Run Scan button.
3. **Range-Bound Opportunities** — status line, ranking/filter popovers,
   the ranked result table, and a skipped-candidates expander.
4. **Selected Strategy** — summary panel + historical chart for whichever
   row you've selected.

It's a dark, compact trading-terminal theme throughout, and the UI is
intentionally thin: it never computes analytics or duplicates
filtering/ranking logic — it only calls the backend's existing public
functions and formats their output.

## Running the application

```
pip install -r requirements.txt
streamlit run ui/app.py
```

You need LSEG Workspace running and an authenticated `lseg.data` session
only when a scan needs to fetch data not already cached locally
(`data/oscill8.db`) — cache-hit scans and chart interactions never touch
LSEG. `core.config.LSEG_SESSION_TYPE` defaults to `"desktop.workspace"`.

## Running the test suite

```
pytest -q
```

As of this writing: **745 passed, 1 skipped**, fully mocked — no live
LSEG session or network access required. Re-run the command yourself for
the current count; don't rely on a number written down here.

- The 1 skip is `tests/test_ui_keyboard_browser.py`, a real-browser
  Playwright test of the grid's Tab/Enter keyboard workflow — it skips
  cleanly when Playwright/Chromium isn't installed, rather than failing.
- `tests/test_live_connection.py` is a separate manual smoke test (not
  part of `pytest -q`) that requires a real, authenticated LSEG
  Workspace session — run it directly on a machine with Workspace open.

## Supported intervals

- `DAILY`
- `HOURLY`
- `4H` (synthesized from `HOURLY` bars — not a native LSEG interval)

Lookback windows are counted in **observations/bars of the selected
interval, not calendar days** — a lookback of 60 on a `4H` scan spans a
different amount of wall-clock time than 60 on `DAILY`.

## Planned: Quanthub provider

Oscill8 does not currently have LSEG entitlement/data for every market
(notably CORRA, and SONIA is not yet fully verified). **Quanthub (QH)**
is being investigated as a secondary, fallback-only data provider for
those gaps — intended to activate automatically, per individual data
request, only when LSEG can't serve it, with LSEG always tried first.

**This is investigation-stage only. No QH code exists in this
repository today**, and using Oscill8 requires no QH setup, credentials,
or configuration. See `CLAUDE.md`'s "Planned: Quanthub Secondary
Market-Data Provider" section for the current state of that
investigation.

## Current limitations

- No cross-market ("intermarket") strategies — a single strategy's legs
  all belong to one market. (A Strategy *Set* can still mix markets
  across its separate rows/entries — see above.)
- No "Real Contract" mode (scanning one specific, hand-picked set of
  dated contracts) — today's scanner always rolls a position-relative
  template across the active contract curve.
- No composite/blended range score — filtering and ranking are always
  transparent, single-metric operations.
- No saved scans or export workflow (a saved Strategy Set captures
  strategy shapes only, not a price window, lookbacks, or results).
- No cloud/server deployment — LSEG access requires a local Workspace
  desktop session.

## Repository structure

```
core/              LSEG downloader, RIC build/parse, futures calendar, market config
database/          SQLite cache (get_history) sitting between core and everything above it
strategy_engine/   StrategyDefinition, rolling contract combinations, historical pricing
range_analytics/   Range-bound and multi-lookback stability measurements
template_scanner/  Templates, candidate universe, scan orchestration, filtering/ranking
strategy_sets/     Named, saved collections of strategy definitions (JSON persistence)
ui/                Streamlit UI -- app.py, controls.py, scan_view.py, results_view.py,
                   chart_view.py, strategy_set_view.py, formatting.py
tests/             Unit tests for every module above (pytest, LSEG mocked)
```

For full architecture detail, module-by-module design notes, and exact
metric definitions/formulas, see `CLAUDE.md`.
