"""
ui package

Module 6A -- Minimal Functional Scanner. A thin Streamlit layer over the
existing, unmodified backend (strategy_engine, range_analytics,
template_scanner). This package never computes analytics, never
duplicates filtering/ranking/derived-metric formulas, and never talks to
LSEG directly -- it only parses UI input into calls against
template_scanner's public API and formats template_scanner's output for
display.

app.py            Entry point / page orchestration.
state.py          Session-state keys for the expensive scan result.
controls.py       Compact scan bar + strategy grid (curve positions as
                   columns, one row per template).
scan_view.py      Run Scan: builds ScanRequest, calls run_scan().
results_view.py   "Range-Bound Opportunities": status, ranking/filters
                   popovers, ranked result grid, row -> ScanCandidateResult
                   selection, Selected Strategy summary, skipped candidates.
formatting.py     Pure helpers: grid-row translation, filter/sort-key
                   construction, ranked-by/rank-column/selection formatting.
"""
