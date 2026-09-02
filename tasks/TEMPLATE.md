---
id: TASK-NNN
title: One-line imperative description of the change
module: "Module X / Module Y"
branch: task/TASK-NNN-kebab-case-slug
allowed_paths:
  - ui/
  - tests/
forbidden_paths:
  - data/
  - core/
  - database/
test_command: "pytest -q tests/"
requires_new_tests: true
allow_doc_updates: [CLAUDE.md, CHANGELOG.md, README.md]
---

# TASK-NNN — <title>

<!--
  Copy this file into tasks/active/TASK-NNN-<slug>.md and fill it in.
  This template itself is never loaded by dev.ps1: the harness only reads
  tasks/active/<TaskId>*.md.

  Front-matter rules enforced by scripts/_Common.ps1 (Read-DevTaskSpec):
    - id, title, branch, test_command, allowed_paths are all REQUIRED.
    - id must equal the task id passed on the command line.
    - branch must match ^task/TASK-\d{3}-[a-z0-9-]+$
    - test_command must scope pytest to tests/ (never a bare 'pytest -q').
    - allowed_paths must be non-empty.

  Section headings below are checked by name. Missing ones are warnings,
  not errors, but write them anyway -- they are the whole contract.

  allowed_paths is VERIFIED AFTER THE FACT, not enforced during editing.
  The harness can detect an out-of-scope edit; it cannot prevent one.
-->

## Context

Which CLAUDE.md module sections to read before starting, named rather than
pasted (CLAUDE.md is ~115 KB; quoting it into a prompt is wasteful). Note any
prior art, related modules, and the architectural rules that apply.

## Objective

What must be true when this task is done. One paragraph.

## In scope

- Concrete, checkable changes.

## Out of scope

- Everything a reasonable reader might otherwise assume is included.

## Acceptance criteria

- [ ] Observable, verifiable statements.
- [ ] Existing public interfaces preserved unless stated otherwise.
- [ ] No new failures relative to the recorded baseline.

## Test expectations

Which test files are expected to change or be added, and what they must cover.

## Known constraints and gotchas

Repository-specific traps relevant to this task (provider routing, cache keys,
Streamlit widget lifecycle, currently-forming bars, and so on).
