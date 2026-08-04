"""
template_scanner package

Module 5A -- Template / Universe Engine. Translates grid-style dense
weight-vector templates into strategy_engine.StrategyDefinition and
rolls them across a market's contract curve into a deduplicated
candidate universe of StrategyInstances.

No market data is fetched here (no database or core.downloader
imports) and no Module 4 (range_analytics) analytics are computed here
-- this package produces candidate StrategyInstances only. Building
StrategyHistory, running analytics, and filtering/ranking candidates
(Module 5B -- Scanner) are separate, not-yet-implemented
responsibilities that will consume this package's output.

SCOPE: same-market templates only. A template today is one market_key
plus a dense weight vector, translated directly into strategy_engine's
existing single-market StrategyDefinition and rolled via its existing
generate_instances(). This is deliberately the only executable
template shape right now -- see templates.py's module docstring for
why intermarket support (legs spanning multiple markets) is expected
to be an additive extension to this package later, not a modification
of the same-market path built here.
"""

from template_scanner.templates import template_from_dense_weights
from template_scanner.universe import (
    dedupe_candidates,
    generate_candidate_universe,
    generate_candidates,
)

__all__ = [
    "template_from_dense_weights",
    "generate_candidates",
    "generate_candidate_universe",
    "dedupe_candidates",
]
