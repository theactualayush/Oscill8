---
id: TASK-001
title: Surface intermarket Strategy Set entries read-only in the Strategy Set panel
module: "Module 7B / Module 9"
branch: task/TASK-001-intermarket-entry-visibility
allowed_paths:
  - ui/
  - tests/
forbidden_paths:
  - data/
  - core/
  - database/
  - strategy_engine/
  - strategy_sets/
  - template_scanner/
  - range_analytics/
test_command: "pytest -q tests/"
requires_new_tests: true
allow_doc_updates: [CLAUDE.md, CHANGELOG.md]
---

# TASK-001 — Surface intermarket Strategy Set entries read-only in the Strategy Set panel

## Context

Read these CLAUDE.md sections first (by name, do not paste them):

- **Module 9 — Intermarket Strategy Engine**, in particular its "Not yet done /
  explicitly out of scope" list and the display-only vs. authoritative
  identity rule.
- **Module 7B — Strategy Set UI Integration (Simplified)**, in particular the
  "No intermarket representation" note and the widget-lifecycle fix.
- **Module 6A**, for how `ui/formatting.py` stays Streamlit-free and
  unit-testable.

Module 9 shipped the intermarket backend in full: `IntermarketStrategySetEntry`
persists in a Strategy Set's JSON under an `intermarket_entries` key, discriminated
by the presence of a `legs` array. Module 7B's grid is single-market only, so
loading such a set today shows the user *nothing at all* for those entries. The
file is not corrupted and the entries are not lost — they are simply invisible,
which is indistinguishable, from the trader's seat, from data loss.

This task closes that specific gap and nothing more. It is deliberately the
smallest useful slice of the deferred "Streamlit UI support for authoring/editing
intermarket Strategy Set entries" roadmap item: **visibility before editability.**

## Objective

When a Strategy Set containing `intermarket_entries` is loaded in the Strategy
Set panel, the trader can see that those entries exist and what they contain,
presented read-only and visually distinct from the editable single-market grid.
No authoring, no editing, no scanning.

## In scope

- A read-only presentation of `StrategySet.intermarket_entries` in the Strategy
  Set panel: entry name, enabled flag, interval, price field, and one row per
  `LegSpec` showing market key, offset and weight.
- A clear indication when a loaded set has intermarket entries, so their
  absence from the editable grid is explained rather than silently confusing.
- Pure formatting helpers in `ui/` with no Streamlit import, following
  `ui/formatting.py`'s existing convention, so the translation is unit-testable.
- Unit tests for those helpers.

## Out of scope

- Creating, editing, deleting or reordering intermarket entries. Hand-editing
  the JSON remains the only authoring route after this task.
- Saving. A save of a set loaded with intermarket entries must preserve them
  exactly as loaded — verify this holds; do not add a new save path.
- Wiring `expand_strategy_set()` / `run_scan_on_instances()` /
  `strategy_sets/execution.py` into any UI button.
- Scanning intermarket strategies, or any change to how they are priced,
  ranked or charted.
- Any change under `strategy_sets/`, `strategy_engine/`, `template_scanner/`,
  `range_analytics/`, `core/` or `database/`. If the task appears to require
  one, stop and report rather than widening scope.

## Acceptance criteria

- [ ] A Strategy Set with only single-market entries renders exactly as it does
      today — no visual or behavioural change.
- [ ] A Strategy Set with intermarket entries displays them read-only, and the
      single-market grid continues to behave unchanged.
- [ ] Load → save → reload of a mixed set leaves `intermarket_entries`
      byte-identical in the JSON file.
- [ ] New formatting helpers import no Streamlit and are covered by unit tests.
- [ ] Composite display labels are used for display only, never for provider
      resolution, cache lookup or bp conversion (Module 9's rule).
- [ ] No new test failures relative to the recorded baseline.

## Test expectations

- New tests for the pure entry → display-rows translation, including a set with
  only intermarket entries, a mixed set, and a set with none.
- A round-trip test proving a mixed set survives load → save → reload with its
  intermarket entries intact.
- Existing `tests/test_ui_strategy_set_*.py` files must keep passing unchanged.

## Known constraints and gotchas

- **Widget lifecycle**: Streamlit forbids writing a widget's own session-state
  key after that widget has been instantiated in the current script run. See
  Module 7B's pending-selection indirection before adding any state.
- **`data/strategy_sets/` is live user data** — untracked, and its only backup
  is the harness snapshot. Never write to it from a test. `RBS_STRATEGY_SETS_DIR`
  is redirected to a sandbox during harness runs; tests must not assume the real
  directory exists.
- **Display resolvers are cosmetic**: `resolve_display_market_key()` /
  `resolve_display_offsets()` produce labels such as `"SOFR/CORRA"`. These must
  never reach `core.providers.resolve_provider`, a cache key, or a bp lookup.
- **`IntermarketStrategySetEntry` rejects `expansion.max_curve_position`** at
  construction — it has no intermarket analogue. Do not surface it as editable.
- Entry names are unique across `entries` and `intermarket_entries` combined.
