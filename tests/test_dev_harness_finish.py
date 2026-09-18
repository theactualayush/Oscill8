"""
tests/test_dev_harness_finish.py

Coverage for the development harness's -Finish mode (TASK-002) and for the
two harness fixes that shipped with it: an authoritative `test_command`, and
an empty known-baseline-failure list.

Structure mirrors the rest of this suite: pure predicates first, then the
mutating git operations against throwaway repositories, then the CLI contract.

SAFETY, restated because these tests are the ones that could do damage if they
were written carelessly: no test here touches the real repository's working
tree, index, branches or remotes. Every mutating call runs inside a repository
created under tmp_path with a local bare `origin`. Nothing reaches the network,
and `gh` is always a stub. See tests/harness_ps.py.

What is deliberately NOT tested here: a full `dev.ps1 -Finish` invocation.
Test-DevRepoState hard-blocks unless <root>/.venv/Scripts/python.exe exists, so
an end-to-end run would need a fabricated virtual environment inside the
throwaway repository -- more machinery, and more ways for the test itself to be
wrong, than the thing it would prove. The stage logic is covered function by
function below; dev.ps1's own wiring is covered at the CLI-contract layer.
"""

from __future__ import annotations

import json

import pytest

from tests.harness_ps import (
    git,
    make_repo,
    ps_quote,
    run_dev,
    run_ps_json,
    write_gh_stub,
)


# ---------------------------------------------------------------------------
# test_command parsing (ConvertTo-DevPytestArgs)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def parsed_commands():
    """Every test_command scenario parsed in ONE PowerShell launch -- each
    launch costs about a second, and these are pure-function assertions."""
    body = r"""
$cases = [ordered]@{
    'default'        = 'pytest -q tests/'
    'python_m'       = 'python -m pytest -q tests/unit -k foo'
    'py_m'           = 'py -m pytest tests/'
    'quoted'         = 'pytest -q tests/ -k "not slow"'
    'semicolon'      = 'pytest -q tests/; echo pwned'
    'pipe'           = 'pytest -q tests/ | more'
    'subshell'       = 'pytest -q $(whoami)'
    'backtick'       = 'pytest -q tests/ `ls`'
    'not_pytest'     = 'powershell -Command Get-Process'
    'own_junitxml'   = 'pytest -q tests/ --junitxml=mine.xml'
    'unterminated'   = 'pytest -q "tests/'
    'empty'          = ''
}
$out = [ordered]@{}
foreach ($name in $cases.Keys) {
    $r = ConvertTo-DevPytestArgs -TestCommand $cases[$name] -JUnitRelativePath '.dev/x.xml'
    $out[$name] = [ordered]@{ IsValid = $r.IsValid; Error = $r.Error; Arguments = @($r.Arguments) }
}
Write-DevProbe $out
"""
    return run_ps_json(body)


def test_default_test_command_parses_to_the_historical_argument_vector(parsed_commands):
    case = parsed_commands["default"]
    assert case["IsValid"] is True
    assert case["Arguments"] == [
        "-m", "pytest", "-q", "tests/", "--junitxml=.dev/x.xml", "-p", "no:cacheprovider",
    ]


def test_python_dash_m_form_is_accepted_and_preserves_declared_arguments(parsed_commands):
    case = parsed_commands["python_m"]
    assert case["IsValid"] is True
    assert case["Arguments"] == [
        "-m", "pytest", "-q", "tests/unit", "-k", "foo",
        "--junitxml=.dev/x.xml", "-p", "no:cacheprovider",
    ]


def test_py_dash_m_form_is_accepted(parsed_commands):
    assert parsed_commands["py_m"]["IsValid"] is True
    assert parsed_commands["py_m"]["Arguments"][:3] == ["-m", "pytest", "tests/"]


def test_quoted_argument_survives_tokenisation_as_one_token(parsed_commands):
    case = parsed_commands["quoted"]
    assert case["IsValid"] is True
    assert "not slow" in case["Arguments"]


@pytest.mark.parametrize("name", ["semicolon", "pipe", "subshell", "backtick"])
def test_shell_metacharacters_are_refused(parsed_commands, name):
    case = parsed_commands[name]
    assert case["IsValid"] is False
    assert "forbidden character" in case["Error"]
    assert case["Arguments"] == []


def test_a_non_pytest_command_is_refused(parsed_commands):
    case = parsed_commands["not_pytest"]
    assert case["IsValid"] is False
    assert "must start with" in case["Error"]


def test_a_caller_supplied_junitxml_is_refused_rather_than_silently_overridden(parsed_commands):
    case = parsed_commands["own_junitxml"]
    assert case["IsValid"] is False
    assert "--junitxml" in case["Error"]


def test_unterminated_quote_and_empty_command_are_refused(parsed_commands):
    assert parsed_commands["unterminated"]["IsValid"] is False
    assert parsed_commands["empty"]["IsValid"] is False


def test_invoke_devtestsuite_reports_a_bad_test_command_without_launching_anything(tmp_path):
    """A refused test_command must surface as a launch error, not as a
    mysterious pytest failure."""
    body = r"""
$r = Invoke-DevTestSuite -RepoRoot {repo} -PythonExe 'python.exe' `
    -JUnitRelativePath '.dev/x.xml' `
    -StdOutPath {out} -StdErrPath {err} `
    -SandboxSqlitePath {db} -SandboxStrategySetsDir {sets} `
    -TestCommand 'rm -rf /'
Write-DevProbe ([ordered]@{{ ExitCode = $r.ExitCode; LaunchError = $r.LaunchError; CommandLine = $r.CommandLine }})
""".format(
        repo=ps_quote(tmp_path),
        out=ps_quote(tmp_path / "out.txt"),
        err=ps_quote(tmp_path / "err.txt"),
        db=ps_quote(tmp_path / "sandbox.db"),
        sets=ps_quote(tmp_path / "sets"),
    )
    result = run_ps_json(body)
    assert result["ExitCode"] is None
    assert result["CommandLine"] is None
    assert "must start with" in result["LaunchError"]


# ---------------------------------------------------------------------------
# Branch gate
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def branch_gate_cases():
    body = r"""
$out = [ordered]@{}
$out['ok']        = Test-DevOnTaskBranch -CurrentBranch 'task/TASK-900-probe' -DeclaredBranch 'task/TASK-900-probe'
$out['main']      = Test-DevOnTaskBranch -CurrentBranch 'main' -DeclaredBranch 'main'
$out['master']    = Test-DevOnTaskBranch -CurrentBranch 'master' -DeclaredBranch 'master'
$out['detached']  = Test-DevOnTaskBranch -CurrentBranch 'HEAD' -DeclaredBranch 'task/TASK-900-probe' -IsDetached $true
$out['mismatch']  = Test-DevOnTaskBranch -CurrentBranch 'task/TASK-901-other' -DeclaredBranch 'task/TASK-900-probe'
Write-DevProbe $out
"""
    return run_ps_json(body)


def test_branch_gate_accepts_the_declared_task_branch(branch_gate_cases):
    case = branch_gate_cases["ok"]
    assert case["IsOnTaskBranch"] is True
    assert case["Blockers"] == []


def test_branch_gate_rejects_main_even_when_the_task_declares_it(branch_gate_cases):
    case = branch_gate_cases["main"]
    assert case["IsOnTaskBranch"] is False
    assert any("never commits to a base branch" in b for b in case["Blockers"])


def test_branch_gate_rejects_master(branch_gate_cases):
    assert branch_gate_cases["master"]["IsOnTaskBranch"] is False


def test_branch_gate_rejects_a_detached_head(branch_gate_cases):
    case = branch_gate_cases["detached"]
    assert case["IsOnTaskBranch"] is False
    assert any("detached" in b for b in case["Blockers"])


def test_branch_gate_rejects_a_branch_that_is_not_the_declared_one(branch_gate_cases):
    case = branch_gate_cases["mismatch"]
    assert case["IsOnTaskBranch"] is False
    assert any("task/TASK-900-probe" in b for b in case["Blockers"])


# ---------------------------------------------------------------------------
# Candidate paths
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def candidate_cases():
    body = r"""
$out = [ordered]@{}

$out['basic'] = Get-DevFinishCandidatePaths -Changes ([pscustomobject]@{
    ChangedPaths  = @('src/module.py', 'tests/test_ok.py')
    StagedTracked = @()
})

# A pre-staged path must be scope-checked like any other, not committed blind.
$out['staged'] = Get-DevFinishCandidatePaths -Changes ([pscustomobject]@{
    ChangedPaths  = @('src/module.py')
    StagedTracked = @('src/other.py')
})

# Protected and declared-scratch paths are subtracted explicitly.
$out['protected'] = Get-DevFinishCandidatePaths -Changes ([pscustomobject]@{
    ChangedPaths  = @('src/module.py', 'data/strategy_sets/x.json', 'test_qh.py')
    StagedTracked = @('data/oscill8.db')
})

$out['empty'] = Get-DevFinishCandidatePaths -Changes ([pscustomobject]@{
    ChangedPaths  = @('data/strategy_sets/x.json')
    StagedTracked = @()
})

Write-DevProbe $out
"""
    return run_ps_json(body)


def test_candidate_paths_are_the_changed_paths(candidate_cases):
    case = candidate_cases["basic"]
    assert sorted(case["CandidatePaths"]) == ["src/module.py", "tests/test_ok.py"]
    assert case["HasCandidates"] is True


def test_already_staged_paths_are_included_so_they_cannot_skip_the_scope_check(candidate_cases):
    case = candidate_cases["staged"]
    assert sorted(case["CandidatePaths"]) == ["src/module.py", "src/other.py"]
    assert case["StagedOnly"] == ["src/other.py"]


def test_protected_and_scratch_paths_are_excluded_from_the_candidate_set(candidate_cases):
    case = candidate_cases["protected"]
    assert case["CandidatePaths"] == ["src/module.py"]
    assert sorted(case["ExcludedProtected"]) == ["data/oscill8.db", "data/strategy_sets/x.json"]
    assert case["ExcludedScratch"] == ["test_qh.py"]


def test_a_change_set_holding_only_protected_paths_yields_no_candidates(candidate_cases):
    assert candidate_cases["empty"]["HasCandidates"] is False
    assert candidate_cases["empty"]["CandidatePaths"] == []


# ---------------------------------------------------------------------------
# Stageable-path validation
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def stageable_cases():
    body = r"""
$paths = @(
    'src/module.py', '.', './', '..', '-A', '-u', '/etc/passwd', 'C:/Windows/system.ini',
    'src/../../escape.py', 'src/*.py', 'src/a?b.py', 'data/strategy_sets/x.json',
    'data/oscill8.db', 'test_qh.py', ''
)
$out = [ordered]@{}
foreach ($p in $paths) {
    $key = $p
    if (-not $key) { $key = '<empty>' }
    $r = Test-DevStageablePath -Path $p
    $out[$key] = [ordered]@{ IsValid = $r.IsValid; Reason = $r.Reason }
}
Write-DevProbe $out
"""
    return run_ps_json(body)


def test_an_ordinary_repository_relative_path_is_stageable(stageable_cases):
    assert stageable_cases["src/module.py"]["IsValid"] is True


@pytest.mark.parametrize(
    "path",
    [".", "./", "..", "-A", "-u", "/etc/passwd", "C:/Windows/system.ini",
     "src/../../escape.py", "src/*.py", "src/a?b.py",
     "data/strategy_sets/x.json", "data/oscill8.db", "test_qh.py", "<empty>"],
)
def test_dangerous_pathspecs_are_refused(stageable_cases, path):
    case = stageable_cases[path]
    assert case["IsValid"] is False, path
    assert case["Reason"]


# ---------------------------------------------------------------------------
# git add -- <explicit paths>
# ---------------------------------------------------------------------------

def test_add_paths_stages_exactly_the_paths_given(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "src" / "new.py").write_text("NEW = True\n", encoding="utf-8")
    (repo / "untouched.txt").write_text("leave me\n", encoding="utf-8")

    body = r"""
$r = Invoke-DevGitAddPaths -RepoRoot {repo} -Paths @('src/module.py', 'src/new.py')
Write-DevProbe ([ordered]@{{ ExitCode = $r.ExitCode; Command = $r.Command; StagedPaths = @($r.StagedPaths) }})
""".format(repo=ps_quote(repo))
    result = run_ps_json(body)

    assert result["ExitCode"] == 0
    assert result["Command"] == "git add -- src/module.py src/new.py"
    staged = git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert sorted(staged) == ["src/module.py", "src/new.py"]
    assert "untouched.txt" not in staged


def test_add_paths_stages_a_deletion(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "src" / "module.py").unlink()

    body = r"""
$r = Invoke-DevGitAddPaths -RepoRoot {repo} -Paths @('src/module.py')
Write-DevProbe ([ordered]@{{ ExitCode = $r.ExitCode }})
""".format(repo=ps_quote(repo))
    assert run_ps_json(body)["ExitCode"] == 0

    staged = git(repo, "diff", "--cached", "--name-status").stdout.strip()
    assert staged.startswith("D")


def test_add_paths_refuses_the_whole_call_when_any_path_is_dangerous(tmp_path):
    """One bad path refuses everything -- never a partial stage of the
    acceptable subset."""
    repo = make_repo(tmp_path)
    (repo / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    body = r"""
$errors = @()
foreach ($bad in @(@('.'), @('-A'), @('-u'), @('data/strategy_sets/x.json'), @('test_qh.py'), @())) {{
    try {{
        $null = Invoke-DevGitAddPaths -RepoRoot {repo} -Paths $bad
        $errors += 'NOT REFUSED: ' + ($bad -join ',')
    }}
    catch {{
        $errors += 'refused'
    }}
}}
Write-DevProbe ([ordered]@{{ Results = @($errors) }})
""".format(repo=ps_quote(repo))
    result = run_ps_json(body)

    assert result["Results"] == ["refused"] * 6
    assert git(repo, "diff", "--cached", "--name-only").stdout.strip() == ""


# ---------------------------------------------------------------------------
# git commit -F <file>
# ---------------------------------------------------------------------------

def test_commit_writes_the_message_and_both_trailers(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    body = r"""
$spec = Read-DevTaskSpec -TaskId 'TASK-900' -RepoRoot {repo}
$message = New-DevCommitMessage -Spec $spec -TaskId 'TASK-900' -Paths @('src/module.py') `
    -TestSummary '3 passed, 0 skipped' -ClaudeSessionId 'abc123'
Write-DevTextFile -Path {msg} -Content $message.Text
$null = Invoke-DevGitAddPaths -RepoRoot {repo} -Paths @('src/module.py')
$commit = Invoke-DevGitCommit -RepoRoot {repo} -MessageFile {msg}
Write-DevProbe ([ordered]@{{
    Committed = $commit.Committed; Sha = $commit.CommitSha
    Subject = $message.Subject; HasSessionId = $message.HasSessionId
}})
""".format(repo=ps_quote(repo), msg=ps_quote(tmp_path / "commit-message.txt"))
    result = run_ps_json(body)

    assert result["Committed"] is True
    assert result["Sha"]
    assert result["HasSessionId"] is True

    message = git(repo, "log", "-1", "--format=%B").stdout
    assert message.splitlines()[0] == "Probe task"
    assert "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>" in message
    assert "Claude-Session: https://claude.ai/code/session_abc123" in message
    assert "Task: TASK-900" in message


def test_commit_message_omits_the_session_trailer_when_no_session_is_known(tmp_path):
    repo = make_repo(tmp_path)
    body = r"""
$spec = Read-DevTaskSpec -TaskId 'TASK-900' -RepoRoot {repo}
$message = New-DevCommitMessage -Spec $spec -TaskId 'TASK-900' -Paths @('src/module.py') -TestSummary 'x'
Write-DevProbe ([ordered]@{{ Text = $message.Text; HasSessionId = $message.HasSessionId }})
""".format(repo=ps_quote(repo))
    result = run_ps_json(body)

    assert result["HasSessionId"] is False
    assert "Claude-Session" not in result["Text"]
    assert "Co-Authored-By" in result["Text"]


def test_commit_fails_when_nothing_is_staged(tmp_path):
    repo = make_repo(tmp_path)
    body = r"""
Write-DevTextFile -Path {msg} -Content "Empty probe`n"
$commit = Invoke-DevGitCommit -RepoRoot {repo} -MessageFile {msg}
Write-DevProbe ([ordered]@{{ Committed = $commit.Committed; ExitCode = $commit.ExitCode }})
""".format(repo=ps_quote(repo), msg=ps_quote(tmp_path / "m.txt"))
    result = run_ps_json(body)

    assert result["Committed"] is False
    assert result["ExitCode"] != 0


def test_last_claude_session_id_is_read_from_the_newest_runclaude_summary(tmp_path):
    repo = make_repo(tmp_path)
    runs = repo / ".dev" / "runs" / "TASK-900"
    for stamp, mode, session in (
        ("20260101-000000Z", "RunClaude", "older-session"),
        ("20260202-000000Z", "RunClaude", "newer-session"),
        ("20260303-000000Z", "Finish", "not-a-claude-session"),
    ):
        run_dir = runs / stamp
        run_dir.mkdir(parents=True)
        (run_dir / "run-summary.json").write_text(
            json.dumps({"Mode": mode, "SessionId": session}), encoding="utf-8"
        )

    body = r"""
$id = Get-DevLastClaudeSessionId -RepoRoot {repo} -TaskId 'TASK-900'
Write-DevProbe ([ordered]@{{ SessionId = $id }})
""".format(repo=ps_quote(repo))
    assert run_ps_json(body)["SessionId"] == "newer-session"


def test_last_claude_session_id_is_null_when_no_runclaude_run_exists(tmp_path):
    repo = make_repo(tmp_path)
    body = r"""
$id = Get-DevLastClaudeSessionId -RepoRoot {repo} -TaskId 'TASK-900'
Write-DevProbe ([ordered]@{{ SessionId = $id }})
""".format(repo=ps_quote(repo))
    assert run_ps_json(body)["SessionId"] is None


# ---------------------------------------------------------------------------
# git push -u origin <branch>
# ---------------------------------------------------------------------------

def _commit_something(repo):
    (repo / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "--", "src/module.py")
    git(repo, "commit", "--quiet", "-m", "probe change")


def test_push_sends_the_branch_to_origin_and_sets_upstream(tmp_path):
    repo = make_repo(tmp_path)
    _commit_something(repo)

    body = r"""
$r = Invoke-DevGitPush -RepoRoot {repo} -BranchName 'task/TASK-900-probe' `
    -StdOutPath {out} -StdErrPath {err} -TimeoutSeconds 120
Write-DevProbe ([ordered]@{{ Pushed = $r.Pushed; ExitCode = $r.ExitCode; Command = $r.Command; TimedOut = $r.TimedOut }})
""".format(repo=ps_quote(repo), out=ps_quote(tmp_path / "p.out"), err=ps_quote(tmp_path / "p.err"))
    result = run_ps_json(body)

    assert result["Pushed"] is True, result
    assert result["Command"] == "git push -u origin task/TASK-900-probe"
    upstream = git(repo, "rev-parse", "--abbrev-ref", "task/TASK-900-probe@{u}").stdout.strip()
    assert upstream == "origin/task/TASK-900-probe"


def test_push_fails_cleanly_when_there_is_no_origin(tmp_path):
    repo = make_repo(tmp_path, with_origin=False)
    _commit_something(repo)

    body = r"""
$r = Invoke-DevGitPush -RepoRoot {repo} -BranchName 'task/TASK-900-probe' `
    -StdOutPath {out} -StdErrPath {err} -TimeoutSeconds 120
Write-DevProbe ([ordered]@{{ Pushed = $r.Pushed; ExitCode = $r.ExitCode }})
""".format(repo=ps_quote(repo), out=ps_quote(tmp_path / "p.out"), err=ps_quote(tmp_path / "p.err"))
    result = run_ps_json(body)

    assert result["Pushed"] is False
    assert result["ExitCode"] != 0


def test_push_refuses_a_protected_or_malformed_branch_name(tmp_path):
    repo = make_repo(tmp_path)
    body = r"""
$refusals = @()
foreach ($name in @('main', 'master', 'not-a-task-branch', 'task/TASK-900-probe; rm')) {{
    try {{
        $null = Invoke-DevGitPush -RepoRoot {repo} -BranchName $name -StdOutPath {out} -StdErrPath {err}
        $refusals += ('NOT REFUSED: ' + $name)
    }}
    catch {{
        $refusals += 'refused'
    }}
}}
Write-DevProbe ([ordered]@{{ Results = @($refusals) }})
""".format(repo=ps_quote(repo), out=ps_quote(tmp_path / "p.out"), err=ps_quote(tmp_path / "p.err"))
    assert run_ps_json(body)["Results"] == ["refused"] * 4


# ---------------------------------------------------------------------------
# GitHub CLI: compare URL, auth, PR creation
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def compare_urls():
    body = r"""
$out = [ordered]@{}
$out['https']   = Get-DevCompareUrl -RemoteUrl 'https://github.com/theactualayush/Oscill8.git' -BranchName 'task/TASK-002-finish-mode'
$out['no_git']  = Get-DevCompareUrl -RemoteUrl 'https://github.com/theactualayush/Oscill8' -BranchName 'task/TASK-002-finish-mode'
$out['ssh']     = Get-DevCompareUrl -RemoteUrl 'git@github.com:theactualayush/Oscill8.git' -BranchName 'task/TASK-002-finish-mode'
$out['base']    = Get-DevCompareUrl -RemoteUrl 'https://github.com/o/r.git' -BranchName 'task/TASK-002-finish-mode' -BaseBranch 'develop'
$out['gitlab']  = Get-DevCompareUrl -RemoteUrl 'https://gitlab.com/o/r.git' -BranchName 'task/TASK-002-finish-mode'
$out['empty']   = Get-DevCompareUrl -RemoteUrl '' -BranchName 'task/TASK-002-finish-mode'
Write-DevProbe $out
"""
    return run_ps_json(body)


def test_compare_url_is_derived_from_an_https_remote(compare_urls):
    expected = "https://github.com/theactualayush/Oscill8/compare/main...task/TASK-002-finish-mode?expand=1"
    assert compare_urls["https"] == expected
    assert compare_urls["no_git"] == expected


def test_compare_url_is_derived_from_an_ssh_remote(compare_urls):
    assert compare_urls["ssh"].startswith("https://github.com/theactualayush/Oscill8/compare/")


def test_compare_url_honours_the_base_branch(compare_urls):
    assert "compare/develop..." in compare_urls["base"]


def test_compare_url_is_null_for_a_non_github_or_missing_remote(compare_urls):
    assert compare_urls["gitlab"] is None
    assert compare_urls["empty"] is None


def test_gh_executable_override_is_honoured_and_absence_is_reported_not_raised(tmp_path):
    stub = write_gh_stub(tmp_path)
    body = r"""
$found = Resolve-DevGhExe
$env:RBS_DEV_GH_EXE = 'C:\nope\definitely-not-here.exe'
$missing = Resolve-DevGhExe
Write-DevProbe ([ordered]@{
    FoundResolved = $found.Resolved
    FoundPath     = $found.Path
    MissingUsedOverride = ($missing.Tried -join '|')
})
"""
    result = run_ps_json(body, env={"RBS_DEV_GH_EXE": str(stub)})
    assert result["FoundResolved"] is True
    assert result["FoundPath"] == str(stub)
    assert "RBS_DEV_GH_EXE" in result["MissingUsedOverride"]


def test_an_unauthenticated_gh_is_reported_as_a_fallback_not_an_error(tmp_path):
    stub = write_gh_stub(tmp_path)
    body = r"""
$gh = Resolve-DevGhExe
$auth = Test-DevGhAuthenticated -GhExe $gh.Path -TimeoutSeconds 60
Write-DevProbe ([ordered]@{ Authenticated = $auth.Authenticated; ExitCode = $auth.ExitCode; Message = $auth.Message })
"""
    result = run_ps_json(
        body, env={"RBS_DEV_GH_EXE": str(stub), "GH_STUB_AUTH_EXIT": "1", "GH_STUB_PR_EXIT": "0"}
    )
    assert result["Authenticated"] is False
    assert "falling back" in result["Message"]


def test_an_authenticated_gh_opens_a_pull_request_and_the_url_is_captured(tmp_path):
    stub = write_gh_stub(tmp_path)
    repo = make_repo(tmp_path)
    body = r"""
$gh = Resolve-DevGhExe
$auth = Test-DevGhAuthenticated -GhExe $gh.Path -TimeoutSeconds 60
Write-DevTextFile -Path {body} -Content "probe body`n"
$pr = New-DevPullRequest -RepoRoot {repo} -BranchName 'task/TASK-900-probe' -Title 'Probe task' `
    -BodyFile {body} -StdOutPath {out} -StdErrPath {err} -GhExe $gh.Path -TimeoutSeconds 60
Write-DevProbe ([ordered]@{{
    Authenticated = $auth.Authenticated; Created = $pr.Created; Url = $pr.Url
    ExitCode = $pr.ExitCode; Command = $pr.Command
}})
""".format(
        repo=ps_quote(repo),
        body=ps_quote(tmp_path / "pr-body.md"),
        out=ps_quote(tmp_path / "pr.out"),
        err=ps_quote(tmp_path / "pr.err"),
    )
    result = run_ps_json(
        body, env={"RBS_DEV_GH_EXE": str(stub), "GH_STUB_AUTH_EXIT": "0", "GH_STUB_PR_EXIT": "0"}
    )
    assert result["Authenticated"] is True
    assert result["Created"] is True
    assert result["Url"] == "https://github.com/probe/repo/pull/7"
    assert "--base main" in result["Command"]


def test_a_failing_gh_pr_create_is_reported_as_not_created(tmp_path):
    stub = write_gh_stub(tmp_path)
    repo = make_repo(tmp_path)
    body = r"""
Write-DevTextFile -Path {body} -Content "probe body`n"
$gh = Resolve-DevGhExe
$pr = New-DevPullRequest -RepoRoot {repo} -BranchName 'task/TASK-900-probe' -Title 'Probe task' `
    -BodyFile {body} -StdOutPath {out} -StdErrPath {err} -GhExe $gh.Path -TimeoutSeconds 60
Write-DevProbe ([ordered]@{{ Attempted = $pr.Attempted; Created = $pr.Created; ExitCode = $pr.ExitCode }})
""".format(
        repo=ps_quote(repo),
        body=ps_quote(tmp_path / "pr-body.md"),
        out=ps_quote(tmp_path / "pr.out"),
        err=ps_quote(tmp_path / "pr.err"),
    )
    result = run_ps_json(
        body, env={"RBS_DEV_GH_EXE": str(stub), "GH_STUB_AUTH_EXIT": "0", "GH_STUB_PR_EXIT": "3"}
    )
    assert result["Attempted"] is True
    assert result["Created"] is False
    assert result["ExitCode"] == 3


def test_pull_request_creation_refuses_a_branch_that_is_not_a_task_branch(tmp_path):
    stub = write_gh_stub(tmp_path)
    repo = make_repo(tmp_path)
    body = r"""
Write-DevTextFile -Path {body} -Content "b`n"
$refused = $false
try {{
    $null = New-DevPullRequest -RepoRoot {repo} -BranchName 'main' -Title 't' -BodyFile {body} `
        -StdOutPath {out} -StdErrPath {err} -GhExe {stub}
}}
catch {{ $refused = $true }}
Write-DevProbe ([ordered]@{{ Refused = $refused }})
""".format(
        repo=ps_quote(repo),
        body=ps_quote(tmp_path / "pr-body.md"),
        out=ps_quote(tmp_path / "pr.out"),
        err=ps_quote(tmp_path / "pr.err"),
        stub=ps_quote(stub),
    )
    assert run_ps_json(body)["Refused"] is True


# ---------------------------------------------------------------------------
# Constants and the safety architecture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def harness_constants():
    body = r"""
Write-DevProbe ([ordered]@{
    ExitCodes            = $DevExitCodes
    KnownBaselineFailures = @($DevKnownBaselineFailures)
    AllowedGit           = @($DevAllowedGitSubcommands)
    DeniedGit            = @($DevDeniedGitTokens)
    Protected            = @($DevProtectedPathPrefixes)
    Scratch              = @($DevScratchPaths)
    ProtectedBranches    = @($DevProtectedBranches)
    PrBase               = $DevPrBaseBranch
})
"""
    return run_ps_json(body)


def test_existing_exit_codes_are_not_renumbered(harness_constants):
    codes = harness_constants["ExitCodes"]
    assert codes["Success"] == 0
    assert codes["Usage"] == 1
    assert codes["Preflight"] == 10
    assert codes["SpecInvalid"] == 20
    assert codes["BranchFailed"] == 30
    assert codes["TestsUnusable"] == 40
    assert codes["ClaudeFailed"] == 50
    assert codes["GateFailed"] == 60
    assert codes["DataViolation"] == 70
    assert codes["PathViolation"] == 80


def test_the_three_new_exit_codes_are_appended(harness_constants):
    codes = harness_constants["ExitCodes"]
    assert codes["CommitFailed"] == 90
    assert codes["PushFailed"] == 100
    assert codes["PrFailed"] == 110


def test_the_stale_known_baseline_failure_is_gone(harness_constants):
    assert harness_constants["KnownBaselineFailures"] == []


def test_the_read_only_git_allow_list_was_not_widened(harness_constants):
    allowed = harness_constants["AllowedGit"]
    assert sorted(allowed) == sorted([
        "rev-parse", "status", "branch", "log", "diff", "ls-files",
        "check-ignore", "remote", "symbolic-ref", "show-ref", "describe",
        "config", "ls-remote",
    ])


def test_the_git_deny_list_was_not_weakened(harness_constants):
    denied = harness_constants["DeniedGit"]
    for token in ("add", "commit", "push", "clean", "stash", "reset", "restore",
                  "checkout", "switch", "merge", "rebase", "cherry-pick", "revert"):
        assert token in denied


def test_protected_and_scratch_declarations_are_intact(harness_constants):
    assert harness_constants["Protected"] == ["data/"]
    assert harness_constants["Scratch"] == ["test_qh.py"]
    assert sorted(harness_constants["ProtectedBranches"]) == ["main", "master"]
    assert harness_constants["PrBase"] == "main"


def test_invoke_devgit_still_refuses_every_mutating_subcommand(tmp_path):
    """The read-only gateway is the architectural guarantee the whole harness
    rests on: the new mutating functions bypass it rather than widening it."""
    repo = make_repo(tmp_path)
    body = r"""
$refusals = @()
foreach ($sub in @('add', 'commit', 'push', 'clean', 'stash', 'reset', 'checkout', 'switch', 'merge')) {{
    try {{
        $null = Invoke-DevGit -RepoRoot {repo} -Arguments @($sub)
        $refusals += ('NOT REFUSED: ' + $sub)
    }}
    catch {{
        $refusals += 'refused'
    }}
}}
Write-DevProbe ([ordered]@{{ Results = @($refusals) }})
""".format(repo=ps_quote(repo))
    assert run_ps_json(body)["Results"] == ["refused"] * 9


# ---------------------------------------------------------------------------
# dev.ps1 CLI contract -- the existing modes must be unchanged
# ---------------------------------------------------------------------------

def _dev(*args) -> tuple:
    """Every invocation here refuses during argument validation, before stage 0
    -- nothing is read, written, snapshotted or mutated in the real
    repository."""
    completed = run_dev(*args)
    return completed.returncode, completed.stdout + completed.stderr


def test_dev_usage_lists_all_four_modes():
    code, output = _dev()
    assert code == 1
    for mode in ("-DryRun", "-CreateBranch", "-RunClaude", "-Finish"):
        assert mode in output


def test_dev_still_refuses_two_modes_at_once():
    code, output = _dev("TASK-002", "-DryRun", "-Finish")
    assert code == 1
    assert "mutually exclusive" in output


def test_dev_still_refuses_no_mode():
    code, output = _dev("TASK-002")
    assert code == 1
    assert "a mode is required" in output


def test_dev_refuses_finish_only_switches_on_other_modes():
    code, output = _dev("TASK-002", "-RunClaude", "-NoPush")
    assert code == 1
    assert "-NoPush and -NoPr apply to -Finish only" in output


def test_dev_still_validates_the_task_id_format():
    code, output = _dev("NOT-A-TASK", "-Finish")
    assert code == 1
    assert "must match TASK-NNN" in output
