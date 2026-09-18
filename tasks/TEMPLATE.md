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
expects_diff: true
allow_doc_updates: [CLAUDE.md, CHANGELOG.md, README.md]
# commit_subject: Optional short subject for the -Finish commit (defaults to title)
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

  test_command is EXECUTED, not decorative: -DryRun, -RunClaude and -Finish all
  run exactly what it declares. It is PARSED into a pytest argument vector, so
  it must start with 'pytest', 'python -m pytest' or 'py -m pytest'; shell
  metacharacters and a caller-supplied --junitxml are refused. The harness runs
  pytest through the repository's own .venv interpreter -- it is not a general
  command runner, and there is no shell for a metacharacter to reach.

  requires_new_tests is ADVISORY. Nothing enforces it; write the tests anyway.

  commit_subject (optional) is the subject line -Finish uses for the commit.
  Without it the task's title is used, untruncated -- an over-long subject is
  warned about, never silently mangled.

  expects_diff (optional, default true) tells -RunClaude whether producing no
  working-tree change is a legitimate outcome. Leave it true for any task that
  should result in code changes: with it true, a silent no-op FAILS the run
  instead of quietly passing. Set it to false only for a genuinely
  investigative task whose deliverable is not a diff.

  Section headings below are checked by name. Missing ones are warnings,
  not errors, but write them anyway -- they are the whole contract.

  allowed_paths is VERIFIED AFTER THE FACT, not enforced during editing.
  The harness can detect an out-of-scope edit; it cannot prevent one. -Finish
  re-checks it against the CURRENT working tree (including anything already
  staged) and refuses to stage a single file if any path falls outside it.

  Lifecycle:
      .\dev.ps1 TASK-NNN -CreateBranch
      .\dev.ps1 TASK-NNN -RunClaude
      # you review the diff
      .\dev.ps1 TASK-NNN -Finish        [-NoPush] [-NoPr]
  Moving this file to tasks/completed/ afterwards is manual and deliberate.
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
