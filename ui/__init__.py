"""
ui package

Module 6A -- Compact Scanner. Module 6B v1 -- Selected Strategy Chart.
Module 7B -- Strategy Set panel. A thin Streamlit layer over the
existing, unmodified backend (strategy_engine, range_analytics,
template_scanner, strategy_sets). This package never computes
analytics, never duplicates filtering/ranking/derived-metric formulas
or Strategy Set persistence/expansion logic, and never talks to LSEG
directly -- it only parses UI input into calls against those packages'
public APIs and formats their output for display.

app.py            Entry point / page orchestration.
state.py          Session-state keys for the expensive scan result and
                   the selected candidate's cached history (Module 6B).
controls.py       Compact scan panel + strategy grid (curve positions as
                   columns, one row per template).
scan_view.py      Run Scan: builds ScanRequest, calls run_scan().
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
strategy_set_state.py       Module 7B: session-state for the Strategy
                             Set panel's selected set + editor draft.
strategy_set_formatting.py   Module 7B: pure helpers -- entries -> display
                             rows, structure labels, grid-row -> new
                             StrategySetEntry (reuses ui.formatting).
strategy_set_view.py         Module 7B: the Strategy Set panel -- select/
                             view/run a saved set, and its add/remove/
                             enable-disable/rename/duplicate/delete/save/
                             create editing controls. Runs a set via the
                             new template_scanner.scanner.run_scan_on_
                             instances() (see that module), never a
                             second scanner implementation.
"""
