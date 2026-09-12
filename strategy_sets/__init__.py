"""
strategy_sets package

Module 7A -- Strategy Set Engine (domain modelling and backend
architecture phase, not analytics and not UI).

A StrategySet is a named, user-defined, serializable collection of
StrategySetEntry objects, representing one trading workflow chosen by
the trader -- e.g. "Churning", "Intermarket Churning", "6M Strategies",
"12M Range Bounds", "Medium Vol", "FOMC", "BoC", "RBA". It is simply a
saved scan configuration: not a folder, not a market, not a template
hierarchy, with no restriction on which markets or shapes its entries
mix.

    model.py           StrategySet, StrategySetEntry, ExpansionSettings,
                         StrategyGroup/StrategyGroupPair (composite
                         "Group A x Group B" selection -- data model
                         only; nothing here generates the combinations)
    serialization.py     StrategySet <-> dict <-> JSON, no filesystem I/O
    repository.py           StrategySetRepository -- save/load/list/
                             duplicate/rename/delete, one JSON file per set
    expansion.py               expand_strategy_set() -> StrategyInstance[]
    composite.py                 resolve_composite_combinations() --
                                   "Group A x Group B" pairs composed
                                   into IntermarketDefinition[] and fed
                                   through Module 9's own, unmodified
                                   intermarket expansion machinery;
                                   is_structurally_zero() drops a pair
                                   whose legs cancel exactly (A - A)
                                   before any expansion/provider work

Pipeline this module introduces (the scanner side is entirely
pre-existing and untouched):

    Today:     Manual Entry                  -> StrategyDefinition[] -> Scanner
    Tomorrow:  StrategySet -> Expansion (here) -> StrategyInstance[] -> Scanner

A StrategySet describes WHAT to scan, never WHEN: contract_start/
contract_end are supplied at expand_strategy_set() call time, shared
across every entry in one call -- exactly matching template_scanner.
scanner.ScanRequest, which likewise carries one contract window shared
across its whole list[StrategyDefinition]. Only max_curve_position/
eligible_rics (genuinely strategy-shape/liquidity-dependent, not a
calendar concept) stay per-entry, on StrategySetEntry.expansion.

Design principles (see the Module 7A design review for full rationale):
    1. Strategy Sets are user-owned -- Oscill8 ships with none by
       default; StrategySetRepository never seeds or requires any
       particular saved set.
    2. StrategyDefinitions are reusable -- the same, unmodified
       strategy_engine.StrategyDefinition is composed inside
       StrategySetEntry.definition; no parallel/duplicate shape model
       was introduced.
    3. Expansion reuses the existing StrategyInstance architecture --
       expand_strategy_set() produces plain strategy_engine.
       StrategyInstance objects via template_scanner.universe
       (Module 5B), unmodified.
    4. No existing scanner logic is duplicated -- rolling/filtering/
       deduplication all delegate to template_scanner.universe as-is,
       and the contract-window/definitions split mirrors ScanRequest's
       own shape rather than diverging from it.
    5. Zero changes to analytics -- nothing under range_analytics/ was
       touched, and this package never imports it.

Out of scope for this phase: Streamlit, a strategy editor UI, scanner
integration (wiring StrategySet output INTO a running scan), true
intermarket (cross-market-leg) strategies, watchlists, alerts, and
deployment.
"""

from strategy_sets.composite import (
    CompositeCombination,
    CompositeResolutionError,
    SourceStrategy,
    composite_labels_by_definition_id,
    aggregate_leg_weights,
    compose_definition,
    expand_combinations,
    is_structurally_zero,
    resolve_composite_combinations,
)
from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import (
    ExpansionSettings,
    IntermarketStrategySetEntry,
    StrategyGroup,
    StrategyGroupPair,
    StrategySet,
    StrategySetEntry,
)
from strategy_sets.repository import StrategySetRepository
from strategy_sets.serialization import (
    SCHEMA_VERSION,
    entry_from_dict,
    entry_to_dict,
    expansion_from_dict,
    expansion_to_dict,
    group_from_dict,
    group_pair_from_dict,
    group_pair_to_dict,
    group_to_dict,
    strategy_set_from_dict,
    strategy_set_from_json,
    strategy_set_to_dict,
    strategy_set_to_json,
)

__all__ = [
    "StrategySet",
    "StrategySetEntry",
    "IntermarketStrategySetEntry",
    "StrategyGroup",
    "StrategyGroupPair",
    "ExpansionSettings",
    "StrategySetRepository",
    "expand_strategy_set",
    "CompositeCombination",
    "CompositeResolutionError",
    "SourceStrategy",
    "compose_definition",
    "aggregate_leg_weights",
    "is_structurally_zero",
    "resolve_composite_combinations",
    "expand_combinations",
    "composite_labels_by_definition_id",
    "SCHEMA_VERSION",
    "entry_to_dict",
    "entry_from_dict",
    "expansion_to_dict",
    "expansion_from_dict",
    "group_to_dict",
    "group_from_dict",
    "group_pair_to_dict",
    "group_pair_from_dict",
    "strategy_set_to_dict",
    "strategy_set_from_dict",
    "strategy_set_to_json",
    "strategy_set_from_json",
]
