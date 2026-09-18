"""
tests/harness_ps.py

Helpers for exercising the PowerShell development harness (dev.ps1 and
scripts/*.ps1) from pytest.

Why pytest and not Pester: this machine has only the Windows-bundled Pester
3.4.0, whose assertion API bears little resemblance to Pester 5's, and the
project's single test gate is `pytest -q tests/`. Driving the PowerShell
functions from Python keeps one runner, keeps the harness's own code inside
the gate it enforces on everything else, and adds no new dependency.

SAFETY: nothing here touches the real repository. Every test that needs a
working tree gets a throwaway one under pytest's tmp_path, with a local bare
repository as `origin`, so the mutating functions (add / commit / push) can be
exercised end to end without a network and without any possibility of writing
to the developer's own checkout. The only thing read from the real repository
is the harness source itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Dot-sourced into every probe, in dev.ps1's own load order. Invoke-ClaudeTask
# is included because Get-DevWorkingTreeChanges and Test-DevChangedPathsInScope
# live there and -Finish reuses both.
_HARNESS_FILES = (
    "_Common.ps1",
    "Test-RepoState.ps1",
    "Protect-DevData.ps1",
    "Invoke-Oscill8Tests.ps1",
    "New-TaskBranch.ps1",
    "Invoke-ClaudeTask.ps1",
    "Complete-DevTask.ps1",
)

_JSON_MARKER = "###JSON###"

POWERSHELL = shutil.which("powershell") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def ps_quote(value) -> str:
    """A PowerShell single-quoted literal. Doubling ' is the whole escape
    rule for single-quoted strings, which is why they are used everywhere
    here: no interpolation, no backtick escapes, no surprises from a Windows
    path full of backslashes."""
    return "'" + str(value).replace("'", "''") + "'"


def _preamble() -> str:
    lines = ["$ErrorActionPreference = 'Stop'"]
    for name in _HARNESS_FILES:
        lines.append(". " + ps_quote(SCRIPTS_DIR / name))
    return "\n".join(lines)


def _child_env(env: dict | None) -> dict:
    child_env = dict(os.environ)
    if env:
        for key, value in env.items():
            if value is None:
                child_env.pop(key, None)
            else:
                child_env[key] = str(value)
    return child_env


def run_ps(body: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """Dot-source the harness, run `body`, return the completed process.

    The probe script is written to the OS temp directory, never into the
    repository being tested -- a stray `_probe.ps1` in the real working tree
    would show up as an untracked file in exactly the `git status` output these
    tests are about.
    """
    script = _preamble() + "\n" + body + "\n"
    handle, script_name = tempfile.mkstemp(suffix=".ps1", prefix="oscill8_probe_")
    os.close(handle)
    script_path = Path(script_name)
    script_path.write_text(script, encoding="utf-8")

    try:
        return subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            cwd=str(cwd or REPO_ROOT),
            env=_child_env(env),
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        script_path.unlink(missing_ok=True)


def run_dev(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the real dev.ps1 as a CHILD PROCESS, so its exit code is the
    process's exit code.

    Dot-sourcing it instead (as run_ps does for the library functions) makes
    `exit <n>` terminate the probe script without that code reaching the
    caller, which silently turned every argument-validation assertion into a
    pass. Only argument-validation paths -- which refuse before stage 0 and
    therefore touch nothing -- are exercised through here.
    """
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO_ROOT / "dev.ps1"),
            *args,
        ],
        cwd=str(REPO_ROOT),
        env=_child_env(env),
        capture_output=True,
        text=True,
        timeout=300,
    )


def run_ps_json(body: str, cwd: Path | None = None, env: dict | None = None):
    """Run `body`, which must emit `Write-DevProbe <object>` exactly once, and
    return the deserialised object."""
    helper = (
        "function Write-DevProbe { param($InputObject) "
        "Write-Output ('" + _JSON_MARKER + "' + ($InputObject | ConvertTo-Json -Depth 8 -Compress)) }\n"
    )
    completed = run_ps(helper + body, cwd=cwd, env=env)
    for line in completed.stdout.splitlines():
        if line.startswith(_JSON_MARKER):
            return json.loads(line[len(_JSON_MARKER):])
    raise AssertionError(
        "probe emitted no JSON.\nSTDOUT:\n{}\nSTDERR:\n{}".format(completed.stdout, completed.stderr)
    )


# ---------------------------------------------------------------------------
# Throwaway repositories
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=120
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            "git {} failed in {}:\n{}\n{}".format(" ".join(args), repo, completed.stdout, completed.stderr)
        )
    return completed


TASK_SPEC_TEMPLATE = """---
id: {task_id}
title: {title}
module: "Probe"
branch: {branch}
allowed_paths:
{allowed}
forbidden_paths:
  - data/
test_command: "{test_command}"
requires_new_tests: false
expects_diff: true
allow_doc_updates: [CLAUDE.md]
---

# {task_id} — {title}

## Context

Throwaway specification used by the harness's own tests.

## Objective

Exist.

## In scope

- Nothing real.

## Out of scope

- Everything else.

## Acceptance criteria

- [ ] Parses.
"""


def make_repo(
    tmp_path: Path,
    task_id: str = "TASK-900",
    branch: str = "task/TASK-900-probe",
    allowed_paths=("src/", "tests/"),
    test_command: str = "pytest -q tests/",
    with_origin: bool = True,
    on_branch: bool = True,
) -> Path:
    """A throwaway repository with one commit on main, a task specification, a
    bare `origin`, and (by default) the task branch checked out."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    git(repo, "init", "--quiet")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "config", "user.name", "Harness Probe")
    git(repo, "config", "user.email", "probe@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")

    (repo / ".gitignore").write_text(".dev/\n", encoding="utf-8")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    spec_dir = repo / "tasks" / "active"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / f"{task_id}-probe.md").write_text(
        TASK_SPEC_TEMPLATE.format(
            task_id=task_id,
            title="Probe task",
            branch=branch,
            allowed=("\n".join("  - " + p for p in allowed_paths)),
            test_command=test_command,
        ),
        encoding="utf-8",
    )

    git(repo, "add", "--", ".gitignore", "src/module.py", "tests/test_ok.py", f"tasks/active/{task_id}-probe.md")
    git(repo, "commit", "--quiet", "-m", "initial")

    if with_origin:
        bare = tmp_path / "origin.git"
        git(tmp_path, "init", "--bare", "--quiet", str(bare))
        git(repo, "remote", "add", "origin", str(bare))

    if on_branch:
        git(repo, "switch", "--quiet", "-c", branch)

    return repo


def write_gh_stub(tmp_path: Path, auth_exit: int = 0, pr_exit: int = 0) -> Path:
    """A stub `gh` so the pull-request paths are deterministic without the real
    GitHub CLI, a network, or an account. Exit codes are supplied through the
    environment so one stub covers every scenario."""
    stub = tmp_path / "gh_stub.cmd"
    stub.write_text(
        "@echo off\r\n"
        "if \"%1\"==\"auth\" exit /b %GH_STUB_AUTH_EXIT%\r\n"
        "if \"%1\"==\"pr\" (\r\n"
        "  echo https://github.com/probe/repo/pull/7\r\n"
        "  exit /b %GH_STUB_PR_EXIT%\r\n"
        ")\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
    )
    return stub
