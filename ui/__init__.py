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
controls.py       Section A/B widgets: scan setup + template grid.
scan_view.py      Section C/D: builds ScanRequest, calls run_scan(),
                   renders the skipped-candidates section.
results_view.py   Section E/F/G: filters, ranking, result grid, and
                   row -> ScanCandidateResult selection.
formatting.py     Pure helpers: ratio parsing, template-row translation,
                   filter/sort-key construction, display formatting.
"""
