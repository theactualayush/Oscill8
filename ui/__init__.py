"""
ui package

Module 6A -- Compact Scanner. Module 6B v1 -- Selected Strategy Chart.
Module 7B -- Strategy Set integration (simplified): "Strategy Templates
is the working strategy grid; a Strategy Set is simply a saved named
version of that grid." One strategy grid, one Run Scan button -- a
loaded Strategy Set becomes ordinary grid rows and is run exactly like
manual entry. Module 8 -- Strategy Set Import (CSV/XLSX upload ->
preview -> Import All). Module 9 -- Strategy Set Scan, a separate,
additive way to run a saved Strategy Set at one chosen interval
without touching the grid. A thin Streamlit layer over the existing,
unmodified backend (strategy_engine, range_analytics, template_scanner,
strategy_sets, strategy_import). This package never computes
analytics, never duplicates filtering/ranking/derived-metric formulas
or Strategy Set persistence logic, and never talks to LSEG directly --
it only parses UI input into calls against those packages' public APIs
and formats their output for display.

app.py            Entry point / page orchestration.
state.py          Session-state keys for the expensive scan result and
                   the selected candidate's cached history (Module 6B).
controls.py       Scan panel (Market/Data, Universe -- automatic active
                   contracts, no manual dates, History defaulting to
                   the last six months, Analytics) + the Strategy
                   Templates grid, with the Strategy Set selector/Save
                   control integrated into that section's own header.
scan_view.py      Run Scan: builds ScanRequest from the grid's current
                   rows (whether typed manually or loaded from a saved
                   Strategy Set -- indistinguishable to this module),
                   calls run_scan().
results_view.py   "Range-Bound Opportunities": status, ranking/filters
                   popovers, ranked result grid, row -> ScanCandidateResult
                   selection, Selected Strategy summary, skipped candidates.
chart_view.py      Module 6B: the Selected Strategy history chart --
                   fetches the selected candidate's price history via
                   the existing build_history() (cache-only, never a
                   fresh LSEG call) and plots it against the candidate's
                   already-computed robust range/median levels.
formatting.py     Pure helpers: grid-row translation, filter/sort-key
                   construction, ranked-by/rank-column/selection formatting.
strategy_set_state.py       Session-state for the Strategy Set selector:
                             which saved set (if any) is loaded, and the
                             pending-selection indirection its widget-
                             lifecycle fix depends on. No separate draft
                             state -- the grid itself is the draft.
strategy_set_formatting.py   Pure helpers -- StrategySet entries <-> grid
                             rows (Label + dense weights), resolving a
                             loaded set's market/interval, and building
                             a new StrategySet directly from the grid's
                             current rows (reuses ui.formatting).
strategy_set_view.py         The Strategy Set selector + Save/"+ New"/
                             Delete controls rendered inside ui.controls'
                             Strategy Templates section -- no separate
                             section, no second table, no second Run
                             button. Delete requires an explicit confirm
                             dialog naming the set before it removes
                             anything.
strategy_set_scan_view.py    Module 9: a SEPARATE, additive way to run a
                             saved Strategy Set -- interval selectbox +
                             Run button, shown only when a set is
                             selected. Applies the chosen interval to a
                             transient copy of the set for that run
                             only (strategy_sets.execution); the grid's
                             own Run Scan / per-row Market-Interval are
                             completely unaffected.
strategy_import_state.py     Module 8: session-state for the Import
                             Strategies panel -- whether it's open, the
                             current in-memory ImportPreview, and the
                             one-shot post-import summary. Never touches
                             StrategySetRepository itself.
strategy_import_formatting.py  Module 8: pure text-formatting helpers
                             for the import preview (market breakdown,
                             per-candidate summary, invalid/unavailable
                             row lines) -- no Streamlit import.
strategy_import_view.py      Module 8: upload -> preview -> Cancel/
                             Import All. commit_import() (the only
                             StrategySetRepository write in the whole
                             strategy_import package) is called from
                             exactly one place, the Import All button.
"""
