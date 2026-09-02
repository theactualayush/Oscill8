<#
.SYNOPSIS
    Oscill8 development harness -- observation and safe branch creation.

.DESCRIPTION
    Two supported execution modes, mutually exclusive:

        .\dev.ps1 TASK-001 -DryRun         read-only observation (unchanged)
        .\dev.ps1 TASK-001 -CreateBranch   read-only checks, then create the
                                           task branch declared by the spec

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

    NOT IMPLEMENTED, BY DESIGN, IN THIS LAYER:
        Claude Code invocation, commit, push, PR creation, the run-to-run
        test-delta gate.

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

.EXAMPLE
    .\dev.ps1 TASK-001 -DryRun

.EXAMPLE
    .\dev.ps1 TASK-001 -CreateBranch

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

    [switch]$CreateBranch
)

$ErrorActionPreference = 'Stop'

function Show-DevUsage {
    Write-Host ''
    Write-Host 'Oscill8 development harness' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  Usage:  .\dev.ps1 <TASK-ID> -DryRun'
    Write-Host '          .\dev.ps1 <TASK-ID> -CreateBranch'
    Write-Host ''
    Write-Host '  -DryRun        Read-only observation. Mutates nothing.'
    Write-Host '  -CreateBranch  Preflight, then create the task branch from main.'
    Write-Host ''
    Write-Host '  Exactly one mode must be supplied. Claude invocation, commit,'
    Write-Host '  push, PR creation and the test-delta gate are not implemented'
    Write-Host '  yet and cannot be triggered from this script.'
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

if ($DryRun -and $CreateBranch) {
    Show-DevUsage
    Write-Host 'ERROR: -DryRun and -CreateBranch are mutually exclusive.' -ForegroundColor Red
    exit 1
}

if (-not $DryRun -and -not $CreateBranch) {
    Show-DevUsage
    Write-Host 'ERROR: a mode is required -- pass either -DryRun or -CreateBranch.' -ForegroundColor Red
    exit 1
}

if ($TaskId -notmatch '^TASK-\d{3}$') {
    Show-DevUsage
    Write-Host "ERROR: task id '$TaskId' must match TASK-NNN (e.g. TASK-001)." -ForegroundColor Red
    exit 1
}

$mode = 'DryRun'
if ($CreateBranch) { $mode = 'CreateBranch' }

$totalStages = 8
if ($mode -eq 'CreateBranch') { $totalStages = 7 }

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
else {
    Write-DevLog 'Oscill8 dev harness -- CREATE BRANCH' 'STEP'
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
else {
    Write-DevLog ("Branch : {0} (candidate)" -f $spec.FrontMatter['branch'])
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

$safety = $null
if ($mode -eq 'CreateBranch') {
    $safety = Test-DevWorkingTreeSafeForBranch -DirtyPaths @($state.DirtyPaths)
    foreach ($allowedEntry in $safety.Allowed) { Write-DevLog ("  allowed: {0}" -f $allowedEntry) }

    if (-not $safety.IsSafe) {
        foreach ($blocker in $safety.Blockers) { Write-DevLog $blocker 'ERROR' }
        Write-DevLog 'REFUSING to create a branch: the working tree carries changes that would ride along onto it.' 'ERROR'
        Write-DevLog 'Resolve them yourself -- commit, revert, or declare them as scratch in $DevScratchPaths.' 'ERROR'
        Write-DevLog 'This harness will not clean, stash, revert or stage anything on your behalf.' 'ERROR'
        Write-DevJsonFile -Path (Join-Path $runDir 'branch-safety.json') -InputObject $safety -Depth 6
        exit $DevExitCodes.Preflight
    }
    Write-DevLog 'Working tree is safe to branch from.' 'OK'
    Write-DevJsonFile -Path (Join-Path $runDir 'branch-safety.json') -InputObject $safety -Depth 6
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
else {

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
else {
    if ($branch.Created) {
        $overallResult = ('PASS (branch created: {0})' -f $branch.BranchName)
        [void]$resultNotes.Add('No test suite was run in this mode. Run the delta gate once it exists, or re-run -DryRun on the new branch for a fresh baseline.')
    }
    else {
        $overallResult = 'FAIL (branch not created)'
        $overallExit = $DevExitCodes.BranchFailed
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
else {
    Add-ReportLine ("# Branch-creation report -- {0}" -f $TaskId)
}
Add-ReportLine ''
Add-ReportLine ("**Result: {0}**" -f $overallResult)
Add-ReportLine ''
if ($mode -eq 'DryRun') {
    Add-ReportLine ("- Mode: ``-DryRun`` (read-only observation layer)")
}
else {
    Add-ReportLine ("- Mode: ``-CreateBranch`` (preflight, then one guarded ``git switch -c``)")
}
Add-ReportLine ("- Generated (UTC): {0}" -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
Add-ReportLine ("- Run directory: ``{0}``" -f (ConvertTo-DevRelativePath -Path $runDir -RepoRoot $repoRoot))
Add-ReportLine ("- Exit code: {0}" -f $overallExit)
Add-ReportLine ''
if ($mode -eq 'DryRun') {
    Add-ReportLine 'Not performed by this layer: branch creation, Claude Code invocation, commit, push, stash, clean, reset, restore, staging, delta gate.'
}
else {
    Add-ReportLine 'Not performed by this layer: Claude Code invocation, commit, push, PR creation, delta gate, test execution. No stash, clean, reset, restore or staging occurs on any path.'
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
else {
    Add-ReportLine 'Protected and declared-scratch paths are allowed to be dirty. DirtyTracked and UnknownUntracked paths refuse the branch.'
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
else {

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
else {
    $artifactNames = $artifactNames + @('branch-safety.json', 'branch.json')
}
foreach ($artifact in $artifactNames) {
    Add-ReportLine ("- ``{0}``" -f $artifact)
}
Add-ReportLine ''

$reportPath = Join-Path $runDir 'report.md'
Write-DevTextFile -Path $reportPath -Content (($lines) -join "`r`n")
Write-DevLog ("Report written: {0}" -f (ConvertTo-DevRelativePath -Path $reportPath -RepoRoot $repoRoot)) 'OK'

$resultLabel = 'DRY RUN RESULT'
if ($mode -eq 'CreateBranch') { $resultLabel = 'BRANCH RESULT' }

Write-Host ''
if ($overallExit -eq $DevExitCodes.Success) {
    Write-DevLog ("{0}: {1}" -f $resultLabel, $overallResult) 'OK'
}
else {
    Write-DevLog ("{0}: {1}" -f $resultLabel, $overallResult) 'ERROR'
}
Write-Host ''

exit $overallExit
