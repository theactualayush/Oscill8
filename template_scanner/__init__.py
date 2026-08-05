"""
template_scanner package

Module 5A -- Template / Universe Engine. Translates grid-style dense
weight-vector templates into strategy_engine.StrategyDefinition and
rolls them across a market's contract curve into a deduplicated
candidate universe of StrategyInstances. No market data is fetched
here (no database or core.downloader imports) and no Module 4
(range_analytics) analytics are computed here -- this part of the
package produces candidate StrategyInstances only.

SCOPE: same-market templates only. A template today is one market_key
plus a dense weight vector, translated directly into strategy_engine's
existing single-market StrategyDefinition and rolled via its existing
generate_instances(). This is deliberately the only executable
template shape right now -- see templates.py's module docstring for
why intermarket support (legs spanning multiple markets) is expected
to be an additive extension to this package later, not a modification
of the same-market path built here.

Module 5B -- Scanner / Analytics Orchestration. Prices a candidate
universe through strategy_engine (Module 3, one shared leg cache per
scan) and measures each resulting history through range_analytics
(Module 4A/4B, unmodified), then offers separate, optional filtering
(filters.py) and transparent multi-key ranking (ranking.py) over the
results -- never a composite/opaque score, never a hard-coded
threshold. run_scan() is the REAL-mode (dated-contract) entry point;
analyze_histories() is the mode-agnostic core it delegates to, taking
already-built StrategyHistory objects so a future candidate source
could call it directly.

Module 5B.1 -- Data Availability Hardening. run_scan() catches exactly
one typed exception around build_history() -- core.downloader.
MarketDataUnavailableError, LSEG's own confirmation that a RIC has no
market data at all -- skips the affected candidate (recorded on
ScanReport.skipped as a SkippedCandidate), and continues the scan.
Every other exception still propagates uncaught -- see scanner.py's
module docstring.
"""

from template_scanner.filters import FilterCriterion, apply_filters
from template_scanner.filters import at_lookback as filter_at_lookback
from template_scanner.filters import stability as filter_stability
from template_scanner.metrics import at_lookback, normalized_crossing_frequency
from template_scanner.ranking import SortKey, rank_results
from template_scanner.scan_results import ScanCandidateResult, results_to_dataframe
from template_scanner.scanner import (
    ScanReport,
    ScanRequest,
    SkippedCandidate,
    analyze_histories,
    run_scan,
)
from template_scanner.templates import template_from_dense_weights
from template_scanner.universe import (
    dedupe_candidates,
    generate_candidate_universe,
    generate_candidates,
)

__all__ = [
    # Module 5A
    "template_from_dense_weights",
    "generate_candidates",
    "generate_candidate_universe",
    "dedupe_candidates",
    # Module 5B
    "ScanRequest",
    "ScanReport",
    "SkippedCandidate",
    "run_scan",
    "analyze_histories",
    "ScanCandidateResult",
    "results_to_dataframe",
    "at_lookback",
    "normalized_crossing_frequency",
    "FilterCriterion",
    "apply_filters",
    "filter_at_lookback",
    "filter_stability",
    "SortKey",
    "rank_results",
]
