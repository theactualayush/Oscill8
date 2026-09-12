<#
.SYNOPSIS
    Oscill8 development harness -- observation, safe branch creation, and
    headless Claude Code task execution.

.DESCRIPTION
    Three supported execution modes, mutually exclusive:

        .\dev.ps1 TASK-001 -DryRun         read-only observation (unchanged)
        .\dev.ps1 TASK-001 -CreateBranch   read-only checks, then create the
                                           task branch declared by the spec
        .\dev.ps1 TASK-001 -RunClaude      preflight, run Claude Code as a
                                           file-only implementation agent,
                                           verify the diff, run the tests

    -DryRun resolves the repository root, reads and validates the task
    specification, inspects (never mutates) Git state, classifies every dirty
    path, snapshots data/strategy_sets/ by SHA-256, runs the pytest suite
    against a throwaway sandbox, verifies the protected data is untouched,
    and writes a report. It mutates nothing.

    -CreateBranch runs the same specification and preflight stages, refuses to
    proceed unless the working tree is safe (see below), then performs the
    harness's single mutating git call -- 'git switch -c <name> <base-sha>' --
    and verifies the result. It does not run the test suite.

    Working-tree safety gate (-CreateBranch only), applying the existing
    classification rules without adding a new taxonomy:
        Protected (data/)     ALLOWED, never touched
        Scratch (declared)    ALLOWED, ignored
        DirtyTracked          REFUSED
        UnknownUntracked      REFUSED
    The remedy for a refusal is always the user's to choose. There is no
    flag, and no code path, that cleans, stashes, reverts or stages anything.

    -RunClaude runs the same specification, preflight and safety-gate stages,
    then launches Claude Code headlessly with FILE TOOLS ONLY
    (Read,Edit,Write,Glob,Grep -- no Bash, no PowerShell, no git execution, no
    web, no subagents), inspects what actually changed, verifies the protected
    data, and runs "pytest -q tests/" itself. Claude never runs the tests and
    has no tool capable of committing, staging or pushing.

    SUCCESS IS NOT THE CLAUDE EXIT CODE. The probe established that Claude
    exits 0 even when it accomplished nothing. A -RunClaude run succeeds only
    when ALL of the following hold, each mapped to its own exit code:

        Claude resolved, launched and did not time out          (else 50)
        Claude's JSON parsed, is_error false                    (else 50)
        permission_denials was empty                            (else 50)
        a working-tree change was produced, when the task
          declares expects_diff (default true)                  (else 50)
        every changed path is inside the task's allowed_paths
          (plus allow_doc_updates)                              (else 80)
        data/strategy_sets/ is byte-identical                   (else 70)
        the suite ran                                           (else 40)
        the suite reported no unexpected failures               (else 60)

    A task may set "expects_diff: false" in its front matter to declare that
    producing no diff is a legitimate outcome; absent the key the default is
    true, so a silent no-op is treated as a failure rather than a pass.

    NOT IMPLEMENTED, BY DESIGN, IN THIS LAYER:
        commit, push, PR creation, retries, the run-to-run test-delta gate.

    NEVER PERFORMED, ANYWHERE:
        git add (in any form), commit, amend, push, pull, fetch, merge,
        rebase, cherry-pick, clean, stash, reset, restore, checkout -- <path>,
        history rewriting, operating on a detached HEAD, or committing to main.

    Every artifact this script writes lands under .dev/, which .gitignore
    excludes.

.PARAMETER TaskId
    Task identifier, e.g. TASK-001. Resolved to tasks/active/<TaskId>*.md.

.PARAMETER DryRun
    Read-only observation mode.

.PARAMETER CreateBranch
    Safe feature-branch creation mode.

.PARAMETER RunClaude
    Headless Claude Code task-execution mode.

.PARAMETER ClaudeBudgetUsd
    Hard spend ceiling passed to Claude as --max-budget-usd. Defaults to
    $DevClaudeDefaultBudgetUsd (overridable via $env:RBS_CLAUDE_BUDGET_USD).

.PARAMETER ClaudeTimeoutSeconds
    Hard wall-clock timeout around the Claude process. Defaults to
    $DevClaudeDefaultTimeoutSeconds (overridable via
    $env:RBS_CLAUDE_TIMEOUT_SECONDS). On timeout the process is terminated,
    the run is marked failed, and all diagnostic artifacts are preserved.

.EXAMPLE
    .\dev.ps1 TASK-001 -DryRun

.EXAMPLE
    .\dev.ps1 TASK-001 -CreateBranch

.EXAMPLE
    .\dev.ps1 TASK-001 -RunClaude

.EXAMPLE
    .\dev.ps1 TASK-001 -RunClaude -ClaudeBudgetUsd 25 -ClaudeTimeoutSeconds 3600

.NOTES
    Windows PowerShell 5.1 compatible: no '&&'/'||', no ternary, no '??'.
    All paths are quoted -- the repository lives under a OneDrive path
    containing spaces.
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$TaskId,

    [switch]$DryRun,

    [switch]$CreateBranch,

    [switch]$RunClaude,

    [double]$ClaudeBudgetUsd = 0,

    [int]$ClaudeTimeoutSeconds = 0
)

$ErrorActionPreference = 'Stop'

function Show-DevUsage {
    Write-Host ''
    Write-Host 'Oscill8 development harness' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  Usage:  .\dev.ps1 <TASK-ID> -DryRun'
    Write-Host '          .\dev.ps1 <TASK-ID> -CreateBranch'
    Write-Host '          .\dev.ps1 <TASK-ID> -RunClaude [-ClaudeBudgetUsd <n>] [-ClaudeTimeoutSeconds <n>]'
    Write-Host ''
    Write-Host '  -DryRun        Read-only observation. Mutates nothing.'
    Write-Host '  -CreateBranch  Preflight, then create the task branch from main.'
    Write-Host '  -RunClaude     Run Claude Code as a file-only implementation agent,'
    Write-Host '                 verify the diff, then run "pytest -q tests/".'
    Write-Host ''
    Write-Host '  Exactly one mode must be supplied. Commit, push, PR creation and'
    Write-Host '  the test-delta gate are not implemented yet and cannot be'
    Write-Host '  triggered from this script.'
    Write-Host ''
}

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

if (-not $TaskId) {
    Show-DevUsage
    Write-Host 'ERROR: no task id supplied.' -ForegroundColor Red
    exit 1
}

$modeCount = 0
if ($DryRun)       { $modeCount = $modeCount + 1 }
if ($CreateBranch) { $modeCount = $modeCount + 1 }
if ($RunClaude)    { $modeCount = $modeCount + 1 }

if ($modeCount -gt 1) {
    Show-DevUsage
    Write-Host 'ERROR: -DryRun, -CreateBranch and -RunClaude are mutually exclusive.' -ForegroundColor Red
    exit 1
}

if ($modeCount -eq 0) {
    Show-DevUsage
    Write-Host 'ERROR: a mode is required -- pass -DryRun, -CreateBranch or -RunClaude.' -ForegroundColor Red
    exit 1
}

if ($TaskId -notmatch '^TASK-\d{3}$') {
    Show-DevUsage
    Write-Host "ERROR: task id '$TaskId' must match TASK-NNN (e.g. TASK-001)." -ForegroundColor Red
    exit 1
}

$mode = 'DryRun'
if ($CreateBranch) { $mode = 'CreateBranch' }
if ($RunClaude)    { $mode = 'RunClaude' }

$totalStages = 8
if ($mode -eq 'CreateBranch') { $totalStages = 7 }
if ($mode -eq 'RunClaude')    { $totalStages = 9 }

# ---------------------------------------------------------------------------
# Stage 0 -- repository root and harness bootstrap
# ---------------------------------------------------------------------------

$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }

. (Join-Path $scriptDir 'scripts\_Common.ps1')
. (Join-Path $scriptDir 'scripts\Test-RepoState.ps1')
. (Join-Path $scriptDir 'scripts\Protect-DevData.ps1')
. (Join-Path $scriptDir 'scripts\Invoke-Oscill8Tests.ps1')
. (Join-Path $scriptDir 'scripts\New-TaskBranch.ps1')
. (Join-Path $scriptDir 'scripts\Invoke-ClaudeTask.ps1')

$repoRoot = Get-DevRepoRoot -StartPath $scriptDir
if (-not $repoRoot) {
    Write-Host "ERROR: could not locate a repository root (no .git found above '$scriptDir')." -ForegroundColor Red
    exit $DevExitCodes.Preflight
}

$runStamp = Get-DevTimestamp
$runDir = Join-Path (Join-Path (Join-Path $repoRoot '.dev\runs') $TaskId) $runStamp
$sandboxDir = Join-Path (Join-Path $repoRoot '.dev\sandbox') $runStamp
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

Initialize-DevLog -Path (Join-Path $runDir 'run.log')

Write-Host ''
if ($mode -eq 'DryRun') {
    Write-DevLog 'Oscill8 dev harness -- DRY RUN (read-only)' 'STEP'
}
elseif ($mode -eq 'CreateBranch') {
    Write-DevLog 'Oscill8 dev harness -- CREATE BRANCH' 'STEP'
}
else {
    Write-DevLog 'Oscill8 dev harness -- RUN CLAUDE (file-only implementation agent)' 'STEP'
}
Write-DevLog "Task            : $TaskId"
Write-DevLog "Repository root : $repoRoot"
Write-DevLog "Run directory   : $(ConvertTo-DevRelativePath -Path $runDir -RepoRoot $repoRoot)"

$overallExit = $DevExitCodes.Success
$overallResult = 'PASS'
$resultNotes = New-Object System.Collections.ArrayList

# ---------------------------------------------------------------------------
# Stage 1 -- task specification (shared)
# ---------------------------------------------------------------------------

Write-DevLog ("Stage 1/{0}  Reading task specification" -f $totalStages) 'STEP'
$spec = Read-DevTaskSpec -TaskId $TaskId -RepoRoot $repoRoot

foreach ($specWarning in $spec.Warnings) { Write-DevLog $specWarning 'WARN' }

if (-not $spec.IsValid) {
    foreach ($specError in $spec.Errors) { Write-DevLog $specError 'ERROR' }
    Write-DevLog 'Task specification is invalid; aborting before any further work.' 'ERROR'
    Write-DevJsonFile -Path (Join-Path $runDir 'task-spec.json') -InputObject $spec
    exit $DevExitCodes.SpecInvalid
}

Write-DevLog ("Spec OK: {0}" -f $spec.RelativePath) 'OK'
Write-DevLog ("Title  : {0}" -f $spec.FrontMatter['title'])
if ($mode -eq 'DryRun') {
    Write-DevLog ("Branch : {0} (NOT created -- dry run)" -f $spec.FrontMatter['branch'])
}
elseif ($mode -eq 'CreateBranch') {
    Write-DevLog ("Branch : {0} (candidate)" -f $spec.FrontMatter['branch'])
}
else {
    Write-DevLog ("Branch : {0} (declared by the task)" -f $spec.FrontMatter['branch'])
}

# A task may declare "expects_diff: false" to say that producing no
# working-tree change is a legitimate outcome. Absent the key the default is
# TRUE, so a silent no-op fails rather than quietly passing.
$expectsDiff = $true
if ($spec.FrontMatter.ContainsKey('expects_diff')) {
    $expectsDiff = -not (([string]$spec.FrontMatter['expects_diff']).Trim() -match '^(?i:false|no|0)$')
}
Write-DevJsonFile -Path (Join-Path $runDir 'task-spec.json') -InputObject $spec

# ---------------------------------------------------------------------------
# Stage 2 -- Git / toolchain preflight (read-only, shared)
# ---------------------------------------------------------------------------

Write-DevLog ("Stage 2/{0}  Inspecting repository state (read-only)" -f $totalStages) 'STEP'
$state = Test-DevRepoState -RepoRoot $repoRoot
Write-DevJsonFile -Path (Join-Path $runDir 'preflight.json') -InputObject $state -Depth 10

foreach ($stateWarning in $state.Warnings) { Write-DevLog $stateWarning 'WARN' }

if (-not $state.IsUsable) {
    foreach ($blocker in $state.Blockers) { Write-DevLog $blocker 'ERROR' }
    Write-DevLog 'Preflight failed; aborting.' 'ERROR'
    exit $DevExitCodes.Preflight
}

Write-DevLog ("Branch  : {0}" -f $state.CurrentBranch) 'OK'
Write-DevLog ("Base SHA: {0}  ({1})" -f $state.BaseShaShort, $state.BaseSubject)
Write-DevLog ("Python  : {0}" -f $state.PythonVersion)

# ---------------------------------------------------------------------------
# Stage 3 -- dirty-path classification (shared) + safety gate (branch mode)
# ---------------------------------------------------------------------------

Write-DevLog ("Stage 3/{0}  Classifying working-tree state" -f $totalStages) 'STEP'
if ($mode -eq 'DryRun') {
    Write-DevLog 'The current tree is intentionally dirty; it IS the baseline observation state.'
}

$classCounts = @{}
foreach ($className in @('Protected', 'Scratch', 'DirtyTracked', 'UnknownUntracked')) {
    $classCounts[$className] = @($state.DirtyPaths | Where-Object { $_.Classification -eq $className }).Count
}
foreach ($entry in $state.DirtyPaths) {
    Write-DevLog ("  [{0}] {1,-16} {2}" -f $entry.StatusCode, $entry.Classification, $entry.Path)
}
Write-DevLog ("Classified {0} dirty path(s): Protected={1} Scratch={2} DirtyTracked={3} UnknownUntracked={4}" -f `
    @($state.DirtyPaths).Count, $classCounts['Protected'], $classCounts['Scratch'], `
    $classCounts['DirtyTracked'], $classCounts['UnknownUntracked'])
Write-DevLog 'No path is being staged, stashed, cleaned, reset or restored.' 'OK'

# Both CreateBranch and RunClaude require the same tree state, for two
# different but equally load-bearing reasons:
#   CreateBranch -- an uncommitted change would ride along onto the new branch.
#   RunClaude    -- an uncommitted change would be indistinguishable afterwards
#                   from a change Claude made, destroying the diff-detection
#                   that this mode's success criterion depends on.
# Protected (data/) and declared-scratch paths are allowed to be dirty in both.
$safety = $null
if ($mode -eq 'CreateBranch' -or $mode -eq 'RunClaude') {
    $safety = Test-DevWorkingTreeSafeForBranch -DirtyPaths @($state.DirtyPaths)
    foreach ($allowedEntry in $safety.Allowed) { Write-DevLog ("  allowed: {0}" -f $allowedEntry) }

    if (-not $safety.IsSafe) {
        foreach ($blocker in $safety.Blockers) { Write-DevLog $blocker 'ERROR' }
        if ($mode -eq 'CreateBranch') {
            Write-DevLog 'REFUSING to create a branch: the working tree carries changes that would ride along onto it.' 'ERROR'
        }
        else {
            Write-DevLog 'REFUSING to run Claude: pre-existing changes would be indistinguishable from Claude output.' 'ERROR'
        }
        Write-DevLog 'Resolve them yourself -- commit, revert, or declare them as scratch in $DevScratchPaths.' 'ERROR'
        Write-DevLog 'This harness will not clean, stash, revert or stage anything on your behalf.' 'ERROR'
        Write-DevJsonFile -Path (Join-Path $runDir 'worktree-safety.json') -InputObject $safety -Depth 6
        exit $DevExitCodes.Preflight
    }
    if ($mode -eq 'CreateBranch') { Write-DevLog 'Working tree is safe to branch from.' 'OK' }
    else { Write-DevLog 'Working tree is clean enough for unambiguous diff attribution.' 'OK' }
    Write-DevJsonFile -Path (Join-Path $runDir 'worktree-safety.json') -InputObject $safety -Depth 6
}

# ---------------------------------------------------------------------------
# Stage 4 -- protected data snapshot (BEFORE, shared)
# ---------------------------------------------------------------------------

Write-DevLog ("Stage 4/{0}  Snapshotting data/strategy_sets/ (SHA-256, read-only)" -f $totalStages) 'STEP'
$strategySetsDir = Join-Path $repoRoot 'data\strategy_sets'
$manifestBefore = New-DevDataSnapshot -DirectoryPath $strategySetsDir -RepoRoot $repoRoot -Label 'before'
Write-DevJsonFile -Path (Join-Path $runDir 'data-manifest.before.json') -InputObject $manifestBefore -Depth 6

if (-not $manifestBefore.Exists) {
    Write-DevLog 'data/strategy_sets/ does not exist; nothing to protect.' 'WARN'
}
else {
    Write-DevLog ("Hashed {0} file(s), {1} bytes total." -f $manifestBefore.FileCount, $manifestBefore.TotalBytes) 'OK'
}

# ---------------------------------------------------------------------------
# Mode-specific stages
# ---------------------------------------------------------------------------

$run = $null
$results = $null
$sandbox = $null
$testsUnusable = $false
$knownFailures = @()
$unexpectedFailures = @()
$branch = $null
$claudeExe = $null
$claudeRun = $null
$claudeResult = $null
$changes = $null
$scopeCheck = $null
$claudeFailures = New-Object System.Collections.ArrayList

if ($mode -eq 'DryRun') {

    # --- Stage 5 -- sandbox for RBS_* redirection --------------------------
    Write-DevLog ("Stage 5/{0}  Creating throwaway .dev sandbox for RBS_* redirection" -f $totalStages) 'STEP'
    $sandbox = New-DevSandbox -SandboxRoot $sandboxDir
    Write-DevLog ("RBS_SQLITE_PATH        -> {0}" -f (ConvertTo-DevRelativePath -Path $sandbox.SqlitePath -RepoRoot $repoRoot))
    Write-DevLog ("RBS_STRATEGY_SETS_DIR  -> {0}" -f (ConvertTo-DevRelativePath -Path $sandbox.StrategySetsDir -RepoRoot $repoRoot))

    # --- Stage 6 -- baseline test suite ------------------------------------
    Write-DevLog ("Stage 6/{0}  Running the Oscill8 test suite (baseline)" -f $totalStages) 'STEP'

    $junitRelative = ('.dev/runs/{0}/{1}/tests.baseline.xml' -f $TaskId, $runStamp)
    $run = Invoke-DevTestSuite `
        -RepoRoot $repoRoot `
        -PythonExe $state.PythonExe `
        -JUnitRelativePath $junitRelative `
        -StdOutPath (Join-Path $runDir 'tests.stdout.txt') `
        -StdErrPath (Join-Path $runDir 'tests.stderr.txt') `
        -SandboxSqlitePath $sandbox.SqlitePath `
        -SandboxStrategySetsDir $sandbox.StrategySetsDir `
        -TimeoutSeconds 1800

    Write-DevLog ("Command : {0}" -f $run.CommandLine)
    Write-DevLog ("Duration: {0}s   pytest exit code: {1}" -f $run.DurationSeconds, $run.ExitCode)

    $results = Read-DevJUnitResults -Path $run.JUnitPath

    if ($run.LaunchError) {
        Write-DevLog ("Failed to launch pytest: {0}" -f $run.LaunchError) 'ERROR'
        $testsUnusable = $true
    }
    if ($run.TimedOut) {
        Write-DevLog 'pytest exceeded the harness timeout and was terminated.' 'ERROR'
        $testsUnusable = $true
    }
    if (-not $results.IsParsed) {
        Write-DevLog ("JUnit results unusable: {0}" -f $results.ParseError) 'ERROR'
        $testsUnusable = $true
    }

    if ($results.IsParsed) {
        $allFailing = @(@($results.FailedNodeIds) + @($results.ErrorNodeIds)) | Sort-Object -Unique
        foreach ($nodeId in $allFailing) {
            if ($DevKnownBaselineFailures -contains $nodeId) { $knownFailures = $knownFailures + $nodeId }
            else { $unexpectedFailures = $unexpectedFailures + $nodeId }
        }

        Write-DevLog ("Tests   : {0} total | {1} passed | {2} failed | {3} errors | {4} skipped" -f `
            $results.Total, $results.Passed, $results.Failed, $results.Errors, $results.Skipped) 'OK'
        foreach ($nodeId in $knownFailures) {
            Write-DevLog ("  known baseline failure : {0}" -f $nodeId) 'WARN'
        }
        foreach ($nodeId in $unexpectedFailures) {
            Write-DevLog ("  UNEXPECTED failure     : {0}" -f $nodeId) 'WARN'
        }
    }

    Write-DevJsonFile -Path (Join-Path $runDir 'tests.summary.json') -InputObject ([pscustomobject]@{
        Run                = $run
        Results            = $results
        KnownFailures      = @($knownFailures)
        UnexpectedFailures = @($unexpectedFailures)
    }) -Depth 8
}
elseif ($mode -eq 'CreateBranch') {

    # --- Stage 5 -- branch preconditions and creation ----------------------
    Write-DevLog ("Stage 5/{0}  Branch preconditions and creation" -f $totalStages) 'STEP'
    Write-DevLog ("Candidate branch: {0}" -f $spec.FrontMatter['branch'])
    Write-DevLog ("Base SHA        : {0}" -f $state.BaseSha)
    Write-DevLog 'Checking name pattern, HEAD identity, local collision and origin collision...'

    $branch = New-DevTaskBranch `
        -RepoRoot $repoRoot `
        -BranchName ([string]$spec.FrontMatter['branch']) `
        -BaseSha ([string]$state.BaseSha) `
        -ExpectedBaseBranch 'main'

    foreach ($branchWarning in $branch.Warnings) { Write-DevLog $branchWarning 'WARN' }
    foreach ($checkName in $branch.Checks.Keys) {
        $checkStatus = 'FAIL'
        if ($branch.Checks[$checkName]) { $checkStatus = 'pass' }
        Write-DevLog ("  {0,-24} {1}" -f $checkName, $checkStatus)
    }
    foreach ($gitCommand in $branch.GitCommands) {
        Write-DevLog ("  executed: {0}" -f $gitCommand)
    }

    Write-DevJsonFile -Path (Join-Path $runDir 'branch.json') -InputObject $branch -Depth 8

    if ($branch.Created) {
        Write-DevLog ("Branch created and verified: {0} @ {1}" -f $branch.PostBranch, $branch.PostSha) 'OK'
    }
    else {
        foreach ($blocker in $branch.Blockers) { Write-DevLog $blocker 'ERROR' }
        Write-DevLog 'Branch was NOT created. Nothing in the working tree was modified.' 'ERROR'
    }
}
else {

    # --- Stage 5 -- Claude preflight and headless execution ----------------
    Write-DevLog ("Stage 5/{0}  Claude preflight and headless execution" -f $totalStages) 'STEP'

    # (a) Recursion guard. Launching Claude from inside a Claude session is
    #     refused outright rather than worked around.
    $recursion = Test-DevClaudeRecursion
    if ($recursion.IsRecursive) {
        Write-DevLog ("Claude session environment detected: {0}" -f ($recursion.Detected -join ', ')) 'ERROR'
        Write-DevLog 'REFUSING to launch a nested Claude process. Run this from a plain terminal.' 'ERROR'
        exit $DevExitCodes.Preflight
    }
    Write-DevLog 'Recursion guard: no parent Claude session detected.' 'OK'

    # (b) Path safety. An 8.3 short-name component makes Claude Code deny file
    #     access while still exiting 0 with is_error false -- an invisible
    #     no-op. Refuse up front instead of discovering it afterwards.
    $longRepoRoot = Resolve-DevLongPath -Path $repoRoot
    $pathCheck = Test-DevPathIsLongForm -Path $longRepoRoot
    if (-not $pathCheck.IsLongForm) {
        Write-DevLog ("Repository path contains an 8.3 short-name component: {0}" -f ($pathCheck.OffendingSegments -join ', ')) 'ERROR'
        Write-DevLog ("Resolved path: {0}" -f $longRepoRoot) 'ERROR'
        Write-DevLog 'Claude Code treats such a path as suspicious and silently denies file access.' 'ERROR'
        exit $DevExitCodes.Preflight
    }
    Write-DevLog ("Repository path is long-form: {0}" -f $longRepoRoot) 'OK'

    # (c) Executable resolution.
    $claudeExe = Resolve-DevClaudeExe
    Write-DevJsonFile -Path (Join-Path $runDir 'claude-exe.json') -InputObject $claudeExe -Depth 5
    if (-not $claudeExe.Resolved) {
        foreach ($tried in $claudeExe.Tried) { Write-DevLog ("  tried: {0}" -f $tried) }
        Write-DevLog $claudeExe.Error 'ERROR'
        exit $DevExitCodes.ClaudeFailed
    }
    Write-DevLog ("Claude   : {0}" -f $claudeExe.Path) 'OK'
    Write-DevLog ("Version  : {0}   (resolved via {1})" -f $claudeExe.Version, $claudeExe.Source)

    # (d) Budget and timeout: explicit parameter beats environment default.
    $effectiveBudget = $DevClaudeDefaultBudgetUsd
    if ($ClaudeBudgetUsd -gt 0) { $effectiveBudget = $ClaudeBudgetUsd }
    $effectiveTimeout = $DevClaudeDefaultTimeoutSeconds
    if ($ClaudeTimeoutSeconds -gt 0) { $effectiveTimeout = $ClaudeTimeoutSeconds }
    $sessionId = [guid]::NewGuid().ToString()
    Write-DevLog ("Budget   : {0} USD   Timeout: {1}s   Session: {2}" -f $effectiveBudget, $effectiveTimeout, $sessionId)

    # (e) Prompt. Written to the run directory and fed over stdin -- never as
    #     a positional argument, because the repo path contains spaces.
    $promptPath = Join-Path $runDir 'prompt.md'
    Write-DevTextFile -Path $promptPath -Content (New-DevClaudePrompt -Spec $spec -RepoRoot $longRepoRoot)
    Write-DevLog ("Prompt   : {0}" -f (ConvertTo-DevRelativePath -Path $promptPath -RepoRoot $repoRoot))
    Write-DevLog 'Tools granted to Claude: Read, Edit, Write, Glob, Grep. No shell, no git, no web, no subagents.'

    # (f) Launch.
    $claudeRun = Invoke-DevClaudeTask `
        -RepoRoot $longRepoRoot `
        -ClaudeExe $claudeExe.Path `
        -PromptPath $promptPath `
        -StdOutPath (Join-Path $runDir 'claude.stdout.json') `
        -StdErrPath (Join-Path $runDir 'claude.stderr.txt') `
        -DebugRelativePath ('.dev/runs/{0}/{1}/claude.debug.log' -f $TaskId, $runStamp) `
        -SessionId $sessionId `
        -MaxBudgetUsd $effectiveBudget `
        -TimeoutSeconds $effectiveTimeout

    Write-DevLog ("Command  : {0}" -f $claudeRun.CommandLine)
    Write-DevLog ("Duration : {0}s   Claude exit code: {1}" -f $claudeRun.DurationSeconds, $claudeRun.ExitCode)

    if ($claudeRun.LaunchError) {
        Write-DevLog ("Failed to launch Claude: {0}" -f $claudeRun.LaunchError) 'ERROR'
        [void]$claudeFailures.Add('Claude could not be launched: ' + $claudeRun.LaunchError)
    }
    if ($claudeRun.TimedOut) {
        Write-DevLog ("Claude exceeded the {0}s timeout and was terminated. Artifacts preserved." -f $effectiveTimeout) 'ERROR'
        [void]$claudeFailures.Add('Claude exceeded the configured wall-clock timeout.')
    }

    # (g) Parse the JSON result -- the authoritative outcome, NOT the exit code.
    $claudeResult = Read-DevClaudeResult -Path $claudeRun.StdOutPath
    Write-DevJsonFile -Path (Join-Path $runDir 'claude.summary.json') -InputObject ([pscustomobject]@{
        Run    = $claudeRun
        Result = $claudeResult
    }) -Depth 8

    if (-not $claudeResult.IsParsed) {
        Write-DevLog ("Claude result unusable: {0}" -f $claudeResult.ParseError) 'ERROR'
        [void]$claudeFailures.Add('Claude output could not be parsed: ' + $claudeResult.ParseError)
    }
    else {
        Write-DevLog ("is_error : {0}   subtype: {1}   turns: {2}   cost: {3} USD" -f `
            $claudeResult.IsError, $claudeResult.Subtype, $claudeResult.NumTurns, $claudeResult.TotalCostUsd)
        Write-DevLog ("Session  : {0}   (resume with: claude --resume {0})" -f $claudeResult.SessionId)

        if ($claudeResult.IsError) {
            Write-DevLog 'Claude reported is_error = true.' 'ERROR'
            [void]$claudeFailures.Add('Claude reported is_error = true.')
        }

        $denialCount = @($claudeResult.PermissionDenials).Count
        if ($denialCount -gt 0) {
            Write-DevLog ("{0} permission denial(s) recorded -- Claude was blocked from tool calls it attempted:" -f $denialCount) 'ERROR'
            foreach ($denial in @($claudeResult.PermissionDenials)) {
                Write-DevLog ("  denied: {0}" -f $denial.tool_name) 'ERROR'
            }
            [void]$claudeFailures.Add("$denialCount permission denial(s) recorded; every clean run has an empty permission_denials array.")
        }
        else {
            Write-DevLog 'permission_denials: empty.' 'OK'
        }
    }

    # --- Stage 6 -- what actually changed ----------------------------------
    Write-DevLog ("Stage 6/{0}  Inspecting the working tree (read-only)" -f $totalStages) 'STEP'
    $changes = Get-DevWorkingTreeChanges -RepoRoot $repoRoot
    Write-DevJsonFile -Path (Join-Path $runDir 'changes.json') -InputObject $changes -Depth 8

    foreach ($path in $changes.ModifiedTracked) { Write-DevLog ("  modified : {0}" -f $path) }
    foreach ($path in $changes.NewUntracked)    { Write-DevLog ("  added    : {0}" -f $path) }
    Write-DevLog ("Changed paths: {0}   (protected paths seen and ignored: {1})" -f `
        @($changes.ChangedPaths).Count, @($changes.ProtectedTouched).Count)

    if (@($changes.StagedTracked).Count -gt 0) {
        Write-DevLog 'Staged changes are present -- Claude has no tool that can stage, so this is unexpected.' 'WARN'
    }

    if ($expectsDiff -and -not $changes.HasChanges) {
        Write-DevLog 'Task declares expects_diff, but Claude produced NO working-tree changes.' 'ERROR'
        [void]$claudeFailures.Add('No repository changes were produced, but the task expects a diff.')
    }
    if (-not $expectsDiff -and -not $changes.HasChanges) {
        Write-DevLog 'No changes produced; the task declares expects_diff: false, so this is a valid outcome.' 'OK'
    }

    # Allowed-path verification. Detection only -- the harness cannot prevent
    # an out-of-scope edit, only refuse to call the run successful.
    $scopeCheck = Test-DevChangedPathsInScope -ChangedPaths @($changes.ChangedPaths) -Spec $spec
    Write-DevJsonFile -Path (Join-Path $runDir 'scope-check.json') -InputObject $scopeCheck -Depth 6
    if (-not $scopeCheck.AllInScope) {
        foreach ($violation in $scopeCheck.Violations) {
            Write-DevLog ("  OUT OF SCOPE: {0}" -f $violation) 'ERROR'
        }
        Write-DevLog ("Permitted: {0}" -f ($scopeCheck.Permitted -join ', ')) 'ERROR'
    }
    elseif ($changes.HasChanges) {
        Write-DevLog 'All changed paths are inside the task allowed_paths.' 'OK'
    }
}

# ---------------------------------------------------------------------------
# Stage 6/7 -- protected data verification (AFTER, shared)
# ---------------------------------------------------------------------------

$verifyStage = 7
if ($mode -eq 'CreateBranch') { $verifyStage = 6 }

Write-DevLog ("Stage {0}/{1}  Verifying data/strategy_sets/ is byte-identical" -f $verifyStage, $totalStages) 'STEP'
$manifestAfter = New-DevDataSnapshot -DirectoryPath $strategySetsDir -RepoRoot $repoRoot -Label 'after'
Write-DevJsonFile -Path (Join-Path $runDir 'data-manifest.after.json') -InputObject $manifestAfter -Depth 6
$dataComparison = Compare-DevDataSnapshot -Before $manifestBefore -After $manifestAfter

if ($dataComparison.Unchanged) {
    Write-DevLog 'Protected data unchanged (hash-for-hash).' 'OK'
}
else {
    foreach ($path in $dataComparison.Added)    { Write-DevLog ("  ADDED    {0}" -f $path) 'ERROR' }
    foreach ($path in $dataComparison.Removed)  { Write-DevLog ("  REMOVED  {0}" -f $path) 'ERROR' }
    foreach ($path in $dataComparison.Modified) { Write-DevLog ("  MODIFIED {0}" -f $path) 'ERROR' }
    Write-DevLog 'PROTECTED DATA VIOLATION: data/strategy_sets/ changed during the run.' 'ERROR'
}

# ---------------------------------------------------------------------------
# Stage 8 -- test suite (RunClaude only; the harness runs it, never Claude)
# ---------------------------------------------------------------------------

if ($mode -eq 'RunClaude') {
    Write-DevLog ("Stage 8/{0}  Running the Oscill8 test suite" -f $totalStages) 'STEP'

    # Same sandbox discipline as -DryRun: RBS_* point into .dev so the suite
    # cannot reach data/oscill8.db or the real Strategy Set JSON files.
    $sandbox = New-DevSandbox -SandboxRoot $sandboxDir
    Write-DevLog ("RBS_SQLITE_PATH        -> {0}" -f (ConvertTo-DevRelativePath -Path $sandbox.SqlitePath -RepoRoot $repoRoot))
    Write-DevLog ("RBS_STRATEGY_SETS_DIR  -> {0}" -f (ConvertTo-DevRelativePath -Path $sandbox.StrategySetsDir -RepoRoot $repoRoot))

    $junitRelative = ('.dev/runs/{0}/{1}/tests.after.xml' -f $TaskId, $runStamp)
    $run = Invoke-DevTestSuite `
        -RepoRoot $repoRoot `
        -PythonExe $state.PythonExe `
        -JUnitRelativePath $junitRelative `
        -StdOutPath (Join-Path $runDir 'tests.stdout.txt') `
        -StdErrPath (Join-Path $runDir 'tests.stderr.txt') `
        -SandboxSqlitePath $sandbox.SqlitePath `
        -SandboxStrategySetsDir $sandbox.StrategySetsDir `
        -TimeoutSeconds 1800

    Write-DevLog ("Command : {0}" -f $run.CommandLine)
    Write-DevLog ("Duration: {0}s   pytest exit code: {1}" -f $run.DurationSeconds, $run.ExitCode)

    $results = Read-DevJUnitResults -Path $run.JUnitPath

    if ($run.LaunchError) {
        Write-DevLog ("Failed to launch pytest: {0}" -f $run.LaunchError) 'ERROR'
        $testsUnusable = $true
    }
    if ($run.TimedOut) {
        Write-DevLog 'pytest exceeded the harness timeout and was terminated.' 'ERROR'
        $testsUnusable = $true
    }
    if (-not $results.IsParsed) {
        Write-DevLog ("JUnit results unusable: {0}" -f $results.ParseError) 'ERROR'
        $testsUnusable = $true
    }

    if ($results.IsParsed) {
        $allFailing = @(@($results.FailedNodeIds) + @($results.ErrorNodeIds)) | Sort-Object -Unique
        foreach ($nodeId in $allFailing) {
            if ($DevKnownBaselineFailures -contains $nodeId) { $knownFailures = $knownFailures + $nodeId }
            else { $unexpectedFailures = $unexpectedFailures + $nodeId }
        }

        Write-DevLog ("Tests   : {0} total | {1} passed | {2} failed | {3} errors | {4} skipped" -f `
            $results.Total, $results.Passed, $results.Failed, $results.Errors, $results.Skipped) 'OK'
        foreach ($nodeId in $knownFailures) {
            Write-DevLog ("  known baseline failure : {0}" -f $nodeId) 'WARN'
        }
        foreach ($nodeId in $unexpectedFailures) {
            Write-DevLog ("  UNEXPECTED failure     : {0}" -f $nodeId) 'ERROR'
        }
    }

    Write-DevJsonFile -Path (Join-Path $runDir 'tests.summary.json') -InputObject ([pscustomobject]@{
        Run                = $run
        Results            = $results
        KnownFailures      = @($knownFailures)
        UnexpectedFailures = @($unexpectedFailures)
    }) -Depth 8
}

# ---------------------------------------------------------------------------
# Final stage -- result determination and report
# ---------------------------------------------------------------------------

Write-DevLog ("Stage {0}/{0}  Writing report" -f $totalStages) 'STEP'

if (-not $dataComparison.Unchanged) {
    $overallResult = 'FAIL (protected data changed)'
    $overallExit = $DevExitCodes.DataViolation
}
elseif ($mode -eq 'DryRun') {
    if ($testsUnusable) {
        $overallResult = 'FAIL (test suite did not produce usable results)'
        $overallExit = $DevExitCodes.TestsUnusable
    }
    elseif ($unexpectedFailures.Count -gt 0) {
        $overallResult = 'PASS (baseline recorded WITH unexpected failures)'
        [void]$resultNotes.Add("$($unexpectedFailures.Count) failing test(s) are not on the known-baseline list. They are recorded as this machine's baseline, not treated as a regression -- there is nothing yet to compare against. Review them before trusting a future delta gate.")
    }
    elseif ($knownFailures.Count -gt 0) {
        $overallResult = 'PASS (known baseline failures only)'
    }
    else {
        $overallResult = 'PASS'
    }
}
elseif ($mode -eq 'CreateBranch') {
    if ($branch.Created) {
        $overallResult = ('PASS (branch created: {0})' -f $branch.BranchName)
        [void]$resultNotes.Add('No test suite was run in this mode. Run the delta gate once it exists, or re-run -DryRun on the new branch for a fresh baseline.')
    }
    else {
        $overallResult = 'FAIL (branch not created)'
        $overallExit = $DevExitCodes.BranchFailed
    }
}
else {
    # -RunClaude precedence, most severe first. Deliberately explicit rather
    # than collapsed into one boolean, so a failing run says WHY it failed.
    #   70 data violation  > 50 Claude outcome > 80 out-of-scope change
    #                      > 40 tests unusable > 60 unexpected test failures
    if ($claudeFailures.Count -gt 0) {
        $overallResult = 'FAIL (Claude did not complete the task successfully)'
        $overallExit = $DevExitCodes.ClaudeFailed
        foreach ($reason in $claudeFailures) { [void]$resultNotes.Add($reason) }
    }
    elseif ($scopeCheck -and -not $scopeCheck.AllInScope) {
        $overallResult = 'FAIL (changes landed outside the task allowed_paths)'
        $overallExit = $DevExitCodes.PathViolation
        [void]$resultNotes.Add('Out-of-scope paths: ' + (@($scopeCheck.Violations) -join ', '))
    }
    elseif ($testsUnusable) {
        $overallResult = 'FAIL (test suite did not produce usable results)'
        $overallExit = $DevExitCodes.TestsUnusable
    }
    elseif ($unexpectedFailures.Count -gt 0) {
        $overallResult = ('FAIL ({0} unexpected test failure(s))' -f $unexpectedFailures.Count)
        $overallExit = $DevExitCodes.GateFailed
    }
    else {
        $overallResult = 'PASS (implementation produced, tests green)'
        if ($knownFailures.Count -gt 0) {
            $overallResult = 'PASS (known baseline failures only)'
        }
        [void]$resultNotes.Add('Nothing has been committed. Review the diff, then commit yourself -- the harness never commits, stages or pushes.')
    }

    if ($claudeResult -and $claudeResult.IsParsed -and $claudeResult.SessionId) {
        [void]$resultNotes.Add('Resume this Claude session for follow-up with: claude --resume ' + $claudeResult.SessionId)
    }
}

if ($mode -eq 'DryRun') {
    if (@($state.DirtyPaths | Where-Object { $_.Classification -eq 'DirtyTracked' }).Count -gt 0) {
        [void]$resultNotes.Add('Pre-existing dirty tracked file(s) recorded as baseline state. The dry-run layer never cleans, stashes or blocks on them.')
    }
}
if (-not $state.DevDirIgnored) {
    [void]$resultNotes.Add('.dev/ is not gitignored; harness artifacts will appear in git status.')
}

# --- report.md ---------------------------------------------------------------
$lines = New-Object System.Collections.ArrayList
function Add-ReportLine { param([string]$Text = '') [void]$lines.Add($Text) }

if ($mode -eq 'DryRun') {
    Add-ReportLine ("# Dry-run report -- {0}" -f $TaskId)
}
elseif ($mode -eq 'CreateBranch') {
    Add-ReportLine ("# Branch-creation report -- {0}" -f $TaskId)
}
else {
    Add-ReportLine ("# Claude task-execution report -- {0}" -f $TaskId)
}
Add-ReportLine ''
Add-ReportLine ("**Result: {0}**" -f $overallResult)
Add-ReportLine ''
if ($mode -eq 'DryRun') {
    Add-ReportLine ("- Mode: ``-DryRun`` (read-only observation layer)")
}
elseif ($mode -eq 'CreateBranch') {
    Add-ReportLine ("- Mode: ``-CreateBranch`` (preflight, then one guarded ``git switch -c``)")
}
else {
    Add-ReportLine ("- Mode: ``-RunClaude`` (headless Claude, file tools only, then tests)")
}
Add-ReportLine ("- Generated (UTC): {0}" -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
Add-ReportLine ("- Run directory: ``{0}``" -f (ConvertTo-DevRelativePath -Path $runDir -RepoRoot $repoRoot))
Add-ReportLine ("- Exit code: {0}" -f $overallExit)
Add-ReportLine ''
if ($mode -eq 'DryRun') {
    Add-ReportLine 'Not performed by this layer: branch creation, Claude Code invocation, commit, push, stash, clean, reset, restore, staging, delta gate.'
}
elseif ($mode -eq 'CreateBranch') {
    Add-ReportLine 'Not performed by this layer: Claude Code invocation, commit, push, PR creation, delta gate, test execution. No stash, clean, reset, restore or staging occurs on any path.'
}
else {
    Add-ReportLine 'Not performed by this layer: commit, push, PR creation, retries, delta gate. Claude was granted file tools only -- no shell, no git execution, no web, no subagents. Nothing has been committed; review the diff yourself.'
}
Add-ReportLine ''

Add-ReportLine '## Repository'
Add-ReportLine ''
Add-ReportLine '| Field | Value |'
Add-ReportLine '| --- | --- |'
Add-ReportLine ("| Repository root | ``{0}`` |" -f $state.RepoRoot)
Add-ReportLine ("| Remote (origin) | {0} |" -f $state.RemoteUrl)
Add-ReportLine ("| Branch before run | ``{0}`` |" -f $state.CurrentBranch)
if ($mode -eq 'CreateBranch' -and $branch -and $branch.PostBranch) {
    Add-ReportLine ("| Branch after run | ``{0}`` |" -f $branch.PostBranch)
}
Add-ReportLine ("| Detached HEAD | {0} |" -f $state.IsDetachedHead)
Add-ReportLine ("| Base SHA | ``{0}`` |" -f $state.BaseSha)
Add-ReportLine ("| Base subject | {0} |" -f $state.BaseSubject)
Add-ReportLine ("| Python | ``{0}`` ({1}) |" -f (ConvertTo-DevRelativePath -Path $state.PythonExe -RepoRoot $repoRoot), $state.PythonVersion)
Add-ReportLine ("| .dev/ gitignored | {0} |" -f $state.DevDirIgnored)
Add-ReportLine ''

Add-ReportLine '## Task specification'
Add-ReportLine ''
Add-ReportLine ("- Source: ``{0}``" -f $spec.RelativePath)
Add-ReportLine ("- Title: {0}" -f $spec.FrontMatter['title'])
Add-ReportLine ("- Module: {0}" -f $spec.FrontMatter['module'])
if ($mode -eq 'DryRun') {
    Add-ReportLine ("- Declared branch: ``{0}`` (not created)" -f $spec.FrontMatter['branch'])
}
else {
    Add-ReportLine ("- Declared branch: ``{0}``" -f $spec.FrontMatter['branch'])
}
if ($mode -eq 'RunClaude') {
    Add-ReportLine ("- expects_diff: {0}" -f $expectsDiff)
}
Add-ReportLine ("- Declared test command: ``{0}``" -f $spec.FrontMatter['test_command'])
Add-ReportLine ("- allowed_paths: {0}" -f ((@($spec.FrontMatter['allowed_paths']) | ForEach-Object { '`' + $_ + '`' }) -join ', '))
if ($spec.Warnings.Count -gt 0) {
    Add-ReportLine ''
    Add-ReportLine 'Specification warnings:'
    Add-ReportLine ''
    foreach ($specWarning in $spec.Warnings) { Add-ReportLine ("- {0}" -f $specWarning) }
}
Add-ReportLine ''

Add-ReportLine '## Git status (porcelain)'
Add-ReportLine ''
if ($mode -eq 'CreateBranch') {
    Add-ReportLine 'Captured before branch creation. Branch creation does not modify the working tree, so this is also its state afterwards.'
    Add-ReportLine ''
}
if ($mode -eq 'RunClaude') {
    Add-ReportLine 'Captured BEFORE Claude ran. See "Working-tree changes" below for what Claude actually produced.'
    Add-ReportLine ''
}
if (@($state.StatusLines).Count -eq 0) {
    Add-ReportLine 'Working tree clean.'
}
else {
    Add-ReportLine '```'
    foreach ($line in $state.StatusLines) { Add-ReportLine $line }
    Add-ReportLine '```'
}
Add-ReportLine ''

Add-ReportLine '## Classified dirty paths'
Add-ReportLine ''
if ($mode -eq 'DryRun') {
    Add-ReportLine 'The working tree is intentionally dirty. This layer classifies and records it; it never cleans, stashes or blocks on it.'
}
elseif ($mode -eq 'CreateBranch') {
    Add-ReportLine 'Protected and declared-scratch paths are allowed to be dirty. DirtyTracked and UnknownUntracked paths refuse the branch.'
}
else {
    Add-ReportLine 'State BEFORE Claude ran. Protected and declared-scratch paths may be dirty; anything else refuses the run, so that changes afterwards are unambiguously attributable to Claude.'
}
Add-ReportLine ''
if (@($state.DirtyPaths).Count -eq 0) {
    Add-ReportLine '_No dirty paths._'
}
else {
    Add-ReportLine '| Status | Path | Classification | Why |'
    Add-ReportLine '| --- | --- | --- | --- |'
    foreach ($entry in $state.DirtyPaths) {
        Add-ReportLine ("| ``{0}`` | ``{1}`` | {2} | {3} |" -f $entry.StatusCode, $entry.Path, $entry.Classification, $entry.Reason)
    }
}
Add-ReportLine ''
Add-ReportLine ("Counts: Protected={0}, Scratch={1}, DirtyTracked={2}, UnknownUntracked={3}" -f `
    $classCounts['Protected'], $classCounts['Scratch'], $classCounts['DirtyTracked'], $classCounts['UnknownUntracked'])
Add-ReportLine ''

Add-ReportLine '## Protected data manifest -- data/strategy_sets/'
Add-ReportLine ''
Add-ReportLine ("- Directory present: {0}" -f $manifestBefore.Exists)
Add-ReportLine ("- Files: {0}" -f $manifestBefore.FileCount)
Add-ReportLine ("- Total bytes: {0}" -f $manifestBefore.TotalBytes)
Add-ReportLine ("- Post-run verification: {0}" -f $(if ($dataComparison.Unchanged) { 'UNCHANGED (hash-for-hash)' } else { 'VIOLATION' }))
Add-ReportLine ''
if ($manifestBefore.FileCount -gt 0) {
    Add-ReportLine '| File | SHA-256 (first 16) | Bytes |'
    Add-ReportLine '| --- | --- | --- |'
    foreach ($file in @($manifestBefore.Files)) {
        $shortHash = $file.Sha256
        if ($shortHash.Length -gt 16) { $shortHash = $shortHash.Substring(0, 16) }
        Add-ReportLine ("| ``{0}`` | ``{1}`` | {2} |" -f $file.RelativePath, $shortHash, $file.Bytes)
    }
    Add-ReportLine ''
}
if (-not $dataComparison.Unchanged) {
    foreach ($path in $dataComparison.Added)    { Add-ReportLine ("- ADDED: ``{0}``" -f $path) }
    foreach ($path in $dataComparison.Removed)  { Add-ReportLine ("- REMOVED: ``{0}``" -f $path) }
    foreach ($path in $dataComparison.Modified) { Add-ReportLine ("- MODIFIED: ``{0}``" -f $path) }
    Add-ReportLine ''
}

if ($mode -eq 'DryRun') {

    Add-ReportLine '## Test summary'
    Add-ReportLine ''
    Add-ReportLine ("- Command: ``{0}``" -f $run.CommandLine)
    Add-ReportLine ("- Working directory: ``{0}``" -f $run.WorkingDirectory)
    Add-ReportLine ("- ``RBS_SQLITE_PATH`` -> ``{0}``" -f (ConvertTo-DevRelativePath -Path $sandbox.SqlitePath -RepoRoot $repoRoot))
    Add-ReportLine ("- ``RBS_STRATEGY_SETS_DIR`` -> ``{0}``" -f (ConvertTo-DevRelativePath -Path $sandbox.StrategySetsDir -RepoRoot $repoRoot))
    Add-ReportLine ("- pytest exit code: {0}" -f $run.ExitCode)
    Add-ReportLine ("- Wall clock: {0}s" -f $run.DurationSeconds)
    Add-ReportLine ("- pytest summary line: {0}" -f $run.PytestSummaryLine)
    Add-ReportLine ''
    if ($results.IsParsed) {
        Add-ReportLine '| Total | Passed | Failed | Errors | Skipped |'
        Add-ReportLine '| --- | --- | --- | --- | --- |'
        Add-ReportLine ("| {0} | {1} | {2} | {3} | {4} |" -f $results.Total, $results.Passed, $results.Failed, $results.Errors, $results.Skipped)
    }
    else {
        Add-ReportLine ("JUnit XML unusable: {0}" -f $results.ParseError)
    }
    Add-ReportLine ''
    if (@($results.SkippedNodeIds).Count -gt 0) {
        Add-ReportLine 'Skipped:'
        Add-ReportLine ''
        foreach ($nodeId in @($results.SkippedNodeIds)) { Add-ReportLine ("- ``{0}``" -f $nodeId) }
        Add-ReportLine ''
    }

    Add-ReportLine '## Baseline failures'
    Add-ReportLine ''
    Add-ReportLine 'Known, environment-specific failures are recorded rather than treated as regressions. There is no prior baseline to compare against on a first dry run -- this run IS the baseline.'
    Add-ReportLine ''
    if ($knownFailures.Count -eq 0 -and $unexpectedFailures.Count -eq 0) {
        Add-ReportLine '_No failing tests._'
        Add-ReportLine ''
    }
    else {
        if ($knownFailures.Count -gt 0) {
            Add-ReportLine 'Known baseline failures:'
            Add-ReportLine ''
            foreach ($nodeId in $knownFailures) { Add-ReportLine ("- ``{0}``" -f $nodeId) }
            Add-ReportLine ''
        }
        if ($unexpectedFailures.Count -gt 0) {
            Add-ReportLine 'Unexpected failures (recorded as baseline, not yet a regression):'
            Add-ReportLine ''
            foreach ($nodeId in $unexpectedFailures) { Add-ReportLine ("- ``{0}``" -f $nodeId) }
            Add-ReportLine ''
        }
    }
}
elseif ($mode -eq 'CreateBranch') {

    Add-ReportLine '## Working-tree safety gate'
    Add-ReportLine ''
    Add-ReportLine ("- Safe to branch: {0}" -f $safety.IsSafe)
    Add-ReportLine ("- Protected (allowed): {0}" -f $safety.ProtectedCount)
    Add-ReportLine ("- Declared scratch (allowed): {0}" -f $safety.ScratchCount)
    Add-ReportLine ("- Dirty tracked (refuses): {0}" -f $safety.DirtyTrackedCount)
    Add-ReportLine ("- Unknown untracked (refuses): {0}" -f $safety.UnknownUntrackedCount)
    Add-ReportLine ''

    Add-ReportLine '## Branch creation'
    Add-ReportLine ''
    Add-ReportLine ("- Branch: ``{0}``" -f $branch.BranchName)
    Add-ReportLine ("- Base branch: ``{0}``" -f $branch.BaseBranch)
    Add-ReportLine ("- Base SHA: ``{0}``" -f $branch.BaseSha)
    Add-ReportLine ("- Created: {0}" -f $branch.Created)
    if ($branch.PostBranch) {
        Add-ReportLine ("- HEAD after: ``{0}`` @ ``{1}``" -f $branch.PostBranch, $branch.PostSha)
    }
    Add-ReportLine ''
    Add-ReportLine '| Precondition | Result |'
    Add-ReportLine '| --- | --- |'
    foreach ($checkName in $branch.Checks.Keys) {
        $checkStatus = 'FAIL'
        if ($branch.Checks[$checkName]) { $checkStatus = 'pass' }
        Add-ReportLine ("| {0} | {1} |" -f $checkName, $checkStatus)
    }
    Add-ReportLine ''
    if ($branch.RemoteCheck) {
        Add-ReportLine ("Origin check -- performed: {0}, reachable: {1}, exists: {2}. {3}" -f `
            $branch.RemoteCheck.Performed, $branch.RemoteCheck.Reachable, $branch.RemoteCheck.Exists, $branch.RemoteCheck.Message)
        Add-ReportLine ''
    }
    Add-ReportLine 'Git commands executed in this stage:'
    Add-ReportLine ''
    Add-ReportLine '```'
    foreach ($gitCommand in $branch.GitCommands) { Add-ReportLine $gitCommand }
    Add-ReportLine '```'
    Add-ReportLine ''
    if ($branch.Blockers.Count -gt 0) {
        Add-ReportLine 'Blockers:'
        Add-ReportLine ''
        foreach ($blocker in $branch.Blockers) { Add-ReportLine ("- {0}" -f $blocker) }
        Add-ReportLine ''
    }
}
else {

    Add-ReportLine '## Claude execution'
    Add-ReportLine ''
    Add-ReportLine '| Field | Value |'
    Add-ReportLine '| --- | --- |'
    Add-ReportLine ("| Executable | ``{0}`` |" -f $claudeExe.Path)
    Add-ReportLine ("| Resolved via | {0} |" -f $claudeExe.Source)
    Add-ReportLine ("| Version | {0} |" -f $claudeExe.Version)
    Add-ReportLine ("| Session ID | ``{0}`` |" -f $claudeRun.SessionId)
    Add-ReportLine ("| Budget ceiling | {0} USD |" -f $claudeRun.MaxBudgetUsd)
    Add-ReportLine ("| Timeout | {0} s |" -f $claudeRun.TimeoutSeconds)
    Add-ReportLine ("| Wall clock | {0} s |" -f $claudeRun.DurationSeconds)
    Add-ReportLine ("| Process exit code | {0} (NOT the success criterion) |" -f $claudeRun.ExitCode)
    Add-ReportLine ("| Timed out | {0} |" -f $claudeRun.TimedOut)
    if ($claudeResult.IsParsed) {
        Add-ReportLine ("| is_error | {0} |" -f $claudeResult.IsError)
        Add-ReportLine ("| subtype | {0} |" -f $claudeResult.Subtype)
        Add-ReportLine ("| terminal_reason | {0} |" -f $claudeResult.TerminalReason)
        Add-ReportLine ("| turns | {0} |" -f $claudeResult.NumTurns)
        Add-ReportLine ("| cost | {0} USD |" -f $claudeResult.TotalCostUsd)
        Add-ReportLine ("| permission_denials | {0} |" -f @($claudeResult.PermissionDenials).Count)
    }
    else {
        Add-ReportLine ("| JSON result | UNUSABLE: {0} |" -f $claudeResult.ParseError)
    }
    Add-ReportLine ''
    Add-ReportLine ("Command: ``{0}``" -f $claudeRun.CommandLine)
    Add-ReportLine ''
    Add-ReportLine 'Tools granted: `Read, Edit, Write, Glob, Grep`. Withheld: Bash, PowerShell, git execution, web access, subagents.'
    Add-ReportLine ''

    if ($claudeResult.IsParsed -and @($claudeResult.PermissionDenials).Count -gt 0) {
        Add-ReportLine 'Permission denials (every clean run has an empty array):'
        Add-ReportLine ''
        foreach ($denial in @($claudeResult.PermissionDenials)) {
            Add-ReportLine ("- ``{0}``" -f $denial.tool_name)
        }
        Add-ReportLine ''
    }

    if ($claudeResult.IsParsed -and $claudeResult.ResultText) {
        Add-ReportLine "Claude's final message:"
        Add-ReportLine ''
        Add-ReportLine '```'
        Add-ReportLine ([string]$claudeResult.ResultText)
        Add-ReportLine '```'
        Add-ReportLine ''
    }

    Add-ReportLine '## Working-tree changes'
    Add-ReportLine ''
    Add-ReportLine ("- Diff detected: {0}" -f $changes.HasChanges)
    Add-ReportLine ("- Task expects a diff: {0}" -f $expectsDiff)
    Add-ReportLine ("- Modified tracked files: {0}" -f @($changes.ModifiedTracked).Count)
    Add-ReportLine ("- New untracked files: {0}" -f @($changes.NewUntracked).Count)
    Add-ReportLine ("- Staged files (expected 0): {0}" -f @($changes.StagedTracked).Count)
    Add-ReportLine ("- Protected paths seen and left untouched: {0}" -f (@($changes.ProtectedTouched) -join ', '))
    Add-ReportLine ''
    if (@($changes.ChangedPaths).Count -gt 0) {
        foreach ($path in @($changes.ModifiedTracked)) { Add-ReportLine ("- modified ``{0}``" -f $path) }
        foreach ($path in @($changes.NewUntracked))    { Add-ReportLine ("- added ``{0}``" -f $path) }
        Add-ReportLine ''
    }
    if (@($changes.DiffStat).Count -gt 0) {
        Add-ReportLine '```'
        foreach ($line in @($changes.DiffStat)) { Add-ReportLine ([string]$line) }
        Add-ReportLine '```'
        Add-ReportLine ''
    }

    Add-ReportLine '## Allowed-path verification'
    Add-ReportLine ''
    Add-ReportLine 'Verification, not enforcement: the harness cannot prevent an out-of-scope edit, only detect it and fail the run.'
    Add-ReportLine ''
    Add-ReportLine ("- All changed paths in scope: {0}" -f $scopeCheck.AllInScope)
    Add-ReportLine ("- Permitted: {0}" -f ((@($scopeCheck.Permitted) | ForEach-Object { '`' + $_ + '`' }) -join ', '))
    if (-not $scopeCheck.AllInScope) {
        Add-ReportLine ''
        Add-ReportLine 'Out-of-scope changes:'
        Add-ReportLine ''
        foreach ($violation in @($scopeCheck.Violations)) { Add-ReportLine ("- ``{0}``" -f $violation) }
    }
    Add-ReportLine ''

    Add-ReportLine '## Test summary'
    Add-ReportLine ''
    if ($run) {
        Add-ReportLine ("- Command: ``{0}``" -f $run.CommandLine)
        Add-ReportLine ("- ``RBS_SQLITE_PATH`` -> ``{0}``" -f (ConvertTo-DevRelativePath -Path $sandbox.SqlitePath -RepoRoot $repoRoot))
        Add-ReportLine ("- ``RBS_STRATEGY_SETS_DIR`` -> ``{0}``" -f (ConvertTo-DevRelativePath -Path $sandbox.StrategySetsDir -RepoRoot $repoRoot))
        Add-ReportLine ("- pytest exit code: {0}" -f $run.ExitCode)
        Add-ReportLine ("- Wall clock: {0}s" -f $run.DurationSeconds)
        Add-ReportLine ("- pytest summary line: {0}" -f $run.PytestSummaryLine)
        Add-ReportLine ''
        if ($results.IsParsed) {
            Add-ReportLine '| Total | Passed | Failed | Errors | Skipped |'
            Add-ReportLine '| --- | --- | --- | --- | --- |'
            Add-ReportLine ("| {0} | {1} | {2} | {3} | {4} |" -f $results.Total, $results.Passed, $results.Failed, $results.Errors, $results.Skipped)
        }
        else {
            Add-ReportLine ("JUnit XML unusable: {0}" -f $results.ParseError)
        }
    }
    else {
        Add-ReportLine '_The test suite was not reached._'
    }
    Add-ReportLine ''
    if ($knownFailures.Count -gt 0) {
        Add-ReportLine 'Known baseline failures (not treated as regressions):'
        Add-ReportLine ''
        foreach ($nodeId in $knownFailures) { Add-ReportLine ("- ``{0}``" -f $nodeId) }
        Add-ReportLine ''
    }
    if ($unexpectedFailures.Count -gt 0) {
        Add-ReportLine 'Unexpected failures:'
        Add-ReportLine ''
        foreach ($nodeId in $unexpectedFailures) { Add-ReportLine ("- ``{0}``" -f $nodeId) }
        Add-ReportLine ''
    }
}

Add-ReportLine '## Overall result'
Add-ReportLine ''
Add-ReportLine ("**{0}** (exit code {1})" -f $overallResult, $overallExit)
Add-ReportLine ''
if ($resultNotes.Count -gt 0) {
    foreach ($note in $resultNotes) { Add-ReportLine ("- {0}" -f $note) }
    Add-ReportLine ''
}
Add-ReportLine '### Artifacts'
Add-ReportLine ''
$artifactNames = @('run.log', 'task-spec.json', 'preflight.json', 'data-manifest.before.json', 'data-manifest.after.json')
if ($mode -eq 'DryRun') {
    $artifactNames = $artifactNames + @('tests.baseline.xml', 'tests.summary.json', 'tests.stdout.txt', 'tests.stderr.txt')
}
elseif ($mode -eq 'CreateBranch') {
    $artifactNames = $artifactNames + @('worktree-safety.json', 'branch.json')
}
else {
    $artifactNames = $artifactNames + @(
        'worktree-safety.json', 'claude-exe.json', 'prompt.md',
        'claude.stdout.json', 'claude.stderr.txt', 'claude.debug.log',
        'claude.summary.json', 'changes.json', 'scope-check.json',
        'tests.after.xml', 'tests.summary.json', 'tests.stdout.txt',
        'tests.stderr.txt', 'run-summary.json'
    )
}
foreach ($artifact in $artifactNames) {
    Add-ReportLine ("- ``{0}``" -f $artifact)
}
Add-ReportLine ''

# Machine-readable companion to report.md. Deliberately carries no
# environment variables, tokens or credentials -- only run facts.
if ($mode -eq 'RunClaude') {
    Write-DevJsonFile -Path (Join-Path $runDir 'run-summary.json') -Depth 8 -InputObject ([pscustomobject]@{
        TaskId            = $TaskId
        Mode              = $mode
        GeneratedUtc      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        RepositoryRoot    = $state.RepoRoot
        Branch            = $state.CurrentBranch
        HeadSha           = $state.BaseSha
        TaskSpec          = $spec.RelativePath
        ExpectsDiff       = $expectsDiff
        ClaudeExePath     = $claudeExe.Path
        ClaudeVersion     = $claudeExe.Version
        ClaudeSource      = $claudeExe.Source
        SessionId         = $claudeRun.SessionId
        BudgetUsd         = $claudeRun.MaxBudgetUsd
        TimeoutSeconds    = $claudeRun.TimeoutSeconds
        ClaudeExitCode    = $claudeRun.ExitCode
        ClaudeTimedOut    = $claudeRun.TimedOut
        ClaudeIsError     = $claudeResult.IsError
        ClaudeParsed      = $claudeResult.IsParsed
        PermissionDenials = @($claudeResult.PermissionDenials)
        DiffDetected      = $changes.HasChanges
        ChangedPaths      = @($changes.ChangedPaths)
        ScopeViolations   = @($scopeCheck.Violations)
        DataUnchanged     = $dataComparison.Unchanged
        TestExitCode      = $(if ($run) { $run.ExitCode } else { $null })
        TestTotal         = $(if ($results) { $results.Total } else { $null })
        TestPassed        = $(if ($results) { $results.Passed } else { $null })
        TestFailed        = $(if ($results) { $results.Failed } else { $null })
        TestSkipped       = $(if ($results) { $results.Skipped } else { $null })
        UnexpectedFailures = @($unexpectedFailures)
        KnownFailures     = @($knownFailures)
        OverallResult     = $overallResult
        ExitCode          = $overallExit
    })
}

$reportPath = Join-Path $runDir 'report.md'
Write-DevTextFile -Path $reportPath -Content (($lines) -join "`r`n")
Write-DevLog ("Report written: {0}" -f (ConvertTo-DevRelativePath -Path $reportPath -RepoRoot $repoRoot)) 'OK'

$resultLabel = 'DRY RUN RESULT'
if ($mode -eq 'CreateBranch') { $resultLabel = 'BRANCH RESULT' }
if ($mode -eq 'RunClaude')    { $resultLabel = 'CLAUDE RUN RESULT' }

Write-Host ''
if ($overallExit -eq $DevExitCodes.Success) {
    Write-DevLog ("{0}: {1}" -f $resultLabel, $overallResult) 'OK'
}
else {
    Write-DevLog ("{0}: {1}" -f $resultLabel, $overallResult) 'ERROR'
}
Write-Host ''

exit $overallExit
