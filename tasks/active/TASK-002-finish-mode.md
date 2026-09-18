---
id: TASK-002
title: Add a safe -Finish mode that tests, verifies, commits, pushes and opens a PR
module: "Dev harness (dev.ps1, scripts/)"
branch: task/TASK-002-finish-mode
allowed_paths:
  - dev.ps1
  - scripts/
  - tests/
  - tasks/
forbidden_paths:
  - data/
  - core/
  - database/
  - strategy_engine/
  - strategy_sets/
  - template_scanner/
  - range_analytics/
  - ui/
  - strategy_import/
test_command: "pytest -q tests/"
requires_new_tests: true
expects_diff: true
allow_doc_updates: [CLAUDE.md, README.md]
---

# TASK-002 — Add a safe `-Finish` mode to the development harness

## Context

Read these before starting:

- `dev.ps1`'s own comment header — the three existing modes, the stage
  layout, and the "NEVER PERFORMED, ANYWHERE" list.
- `scripts/_Common.ps1` — `$DevExitCodes`, `Invoke-DevGit`'s read-only
  allow-list and deny-list, `$DevProtectedPathPrefixes`, `$DevScratchPaths`.
- `scripts/New-TaskBranch.ps1` — `Invoke-DevGitCreateBranch`, the established
  pattern for a mutating git call: fixed argument vector built inside the
  function, own input re-validation, deliberately NOT routed through
  `Invoke-DevGit`.
- `scripts/Invoke-ClaudeTask.ps1` — `Get-DevWorkingTreeChanges` and
  `Test-DevChangedPathsInScope`.
- `scripts/Invoke-Oscill8Tests.ps1` — the sandboxed pytest runner.

The harness automates TASK → branch → Claude → tests. Everything after that
(stage, commit, push, PR) is manual. This task automates that tail, without
weakening any existing guarantee and without removing the human review gate.

## Objective

`.\dev.ps1 TASK-00N -Finish` runs after a human has reviewed the diff Claude
produced. It re-validates everything from scratch, runs the task's declared
test command, stages only scope-verified paths, commits with the project's
trailers, pushes, and opens a PR when GitHub CLI is available — refusing
before any mutation if any gate fails.

## In scope

- A fourth mutually exclusive mode `-Finish`, with `-NoPush` and `-NoPr`.
- A branch gate: HEAD must be on the task's declared branch, never
  `main`/`master`, never detached.
- Re-inspection of the CURRENT working tree (never a cached earlier result),
  including staged-but-unreviewed paths.
- Scope verification of every candidate path before any mutation.
- Execution of the task's declared `test_command`, in the existing sandbox.
- Protected-data verification before and after.
- Explicit-pathspec staging, commit from a message file, push, optional PR.
- New exit codes `CommitFailed = 90`, `PushFailed = 100`, `PrFailed = 110`,
  appended without renumbering.
- Making `test_command` authoritative for `-DryRun`, `-RunClaude` and
  `-Finish`, via a pytest-only parser (never generic shell execution).
- Removing the stale `$DevKnownBaselineFailures` entry.
- pytest coverage for every gate and both gh paths.
- Documentation: `CLAUDE.md`, `README.md`, `tasks/TEMPLATE.md`.

## Out of scope

- Chaining `-CreateBranch` → `-RunClaude` → `-Finish` into one command.
- Retries, resume loops, automatic merge, rebase, and test-delta gating.
- Moving task files into `tasks/completed/`.
- Any change to application code (`core/`, `database/`, `strategy_engine/`,
  `strategy_sets/`, `template_scanner/`, `range_analytics/`, `ui/`,
  `strategy_import/`).

## Acceptance criteria

- [ ] `-DryRun`, `-CreateBranch` and `-RunClaude` behave as before.
- [ ] `Invoke-DevGit` stays read-only; the allow-list and deny-list are
      unchanged.
- [ ] Every mutating git call is its own function with a fixed argument
      vector and its own input validation.
- [ ] `git add` is only ever reached with explicit `--`-terminated pathspecs;
      `.`, `-A` and `-u` are structurally unreachable.
- [ ] `data/` and `test_qh.py` can never be staged.
- [ ] An out-of-scope change, a failing test run, a wrong branch, a detached
      HEAD or an empty diff each refuse before anything is staged.
- [ ] A missing or unauthenticated `gh` is a successful finish with
      `PrCreated = false` and a compare URL.
- [ ] Existing exit codes keep their numbers.

## Test expectations

`tests/test_dev_harness_finish.py` (with `tests/harness_ps.py` helpers) drives
the PowerShell functions against throwaway git repositories under `tmp_path`,
with a local bare repository as `origin`. No test touches the real repository,
and no test reaches the network.

## Known constraints and gotchas

- Windows PowerShell 5.1: no `&&`/`||`, no ternary, no `??`. Quote every path;
  the repository lives under a OneDrive path containing spaces.
- `Get-DevWorkingTreeChanges.ChangedPaths` omits staged-only changes; `-Finish`
  must union `StagedTracked` in before the scope check, or a pre-staged path
  would be committed unverified.
- `data/` is entirely untracked today, but the staging function must exclude
  protected/scratch paths explicitly rather than relying on that.
- `GIT_TERMINAL_PROMPT=0` suppresses terminal prompts only; the configured
  credential helper is GUI-based, so the push also needs `GCM_INTERACTIVE` and
  `GIT_ASKPASS` plus a wall-clock timeout.
- `dev.ps1`'s mode dispatch uses `else` to mean `-RunClaude` in several places;
  those must become explicit before a fourth mode is added.
