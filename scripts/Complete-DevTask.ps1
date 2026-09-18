<#
.SYNOPSIS
    Commit / push / pull-request layer for the Oscill8 development harness --
    everything the -Finish mode does after its gates have passed.

.DESCRIPTION
    Dot-source alongside _Common.ps1, Test-RepoState.ps1 and
    Invoke-ClaudeTask.ps1:

        . .\scripts\_Common.ps1
        . .\scripts\Test-RepoState.ps1
        . .\scripts\Invoke-ClaudeTask.ps1
        . .\scripts\Complete-DevTask.ps1

    THIS FILE HOLDS THE ONLY MUTATING GIT CALLS IN THE HARNESS BESIDES BRANCH
    CREATION (scripts/New-TaskBranch.ps1). It follows that file's established
    pattern exactly, and for the same reasons:

      * Each mutating operation is its own single-purpose function.
      * Each builds its fixed argument vector INSIDE the function. There is no
        parameter through which a caller could append, replace or inject
        another argument or subcommand.
      * Each re-validates its own inputs, independently of whatever the caller
        claims to have checked.
      * None is routed through Invoke-DevGit, whose allow-list stays strictly
        read-only so no future caller can reach a mutating subcommand through
        the general-purpose wrapper.

    The three operations are exactly:

        git add -- <path> [<path> ...]      Invoke-DevGitAddPaths
        git commit -F <message-file>        Invoke-DevGitCommit
        git push -u origin <branch>         Invoke-DevGitPush

    NEVER PERFORMED HERE, ON ANY PATH: 'git add .', 'git add -A', 'git add -u',
    a glob or wildcard pathspec, clean, stash, reset, restore, checkout,
    revert, merge, rebase, cherry-pick, amend, tag, any --force or
    --force-with-lease, any refspec other than the branch's own name, any
    history rewriting, and any write under data/ or to test_qh.py.

    STAGING IS EXPLICIT AND TWICE-FILTERED. The caller passes a path list that
    has already been scope-checked against the task's allowed_paths;
    Invoke-DevGitAddPaths then re-validates every element again -- rejecting
    protected prefixes, declared scratch paths, absolute paths, traversal,
    option-looking tokens and wildcards -- before it will build a vector. A
    path that reaches 'git add' has therefore passed two independent filters
    written at different layers.

    PULL REQUESTS ARE OPTIONAL AND NEVER MERGED. GitHub CLI absence or a
    signed-out gh is a normal, successful outcome: the run reports
    PrCreated = false and prints a compare URL derived from origin. Only a gh
    that is present AND authenticated AND then fails is an error.

.NOTES
    Windows PowerShell 5.1 compatible: no '&&'/'||', no ternary, no '??'.
    All paths are quoted -- the repository lives under a OneDrive path
    containing spaces.
#>

# Characters that make a pathspec magic to git, or make a token look like an
# option. A path containing one is refused rather than escaped: every path the
# harness stages comes from 'git status'/'git diff' output, so none of them can
# legitimately contain these.
$DevPathSpecForbiddenChars = @('*', '?', '[', ']', ':', "`n", "`r")

# Test seam: when set, Resolve-DevGhExe returns this path instead of probing.
# Exists so the pull-request paths can be exercised against a stub without a
# real GitHub CLI or any network access. Never consulted in normal operation.
$DevGhExeOverrideVar = 'RBS_DEV_GH_EXE'

# Candidate install locations probed when 'gh' is not on PATH.
$DevGhCandidatePaths = @(
    (Join-Path $env:ProgramFiles 'GitHub CLI\gh.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'GitHub CLI\gh.exe'),
    (Join-Path $env:LOCALAPPDATA 'GitHubCLI\bin\gh.exe'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\gh.exe')
)

# ---------------------------------------------------------------------------
# Gate: are we on the task's own branch?
# ---------------------------------------------------------------------------

function Test-DevOnTaskBranch {
    <#
    .SYNOPSIS
        Decide whether HEAD is somewhere -Finish may safely operate.

    .DESCRIPTION
        Three independent refusals, each reported separately so the message
        says which one fired:

            detached HEAD        -- there is no branch to push or to name.
            main / master        -- the harness never commits to a base branch
                                    ($DevProtectedBranches), whatever a task
                                    file happens to declare.
            branch mismatch      -- HEAD is not the branch this task declared,
                                    so the diff under review may not be this
                                    task's work at all.

        Pure predicate. Nothing here switches, creates or modifies a branch --
        the remedy is always the user's to choose.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][AllowNull()][string]$CurrentBranch,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$DeclaredBranch,
        [bool]$IsDetached = $false
    )

    $blockers = New-Object System.Collections.ArrayList

    if ($IsDetached -or -not $CurrentBranch -or $CurrentBranch -eq 'HEAD') {
        [void]$blockers.Add('HEAD is detached; -Finish needs a named branch to commit and push.')
    }
    else {
        if ($DevProtectedBranches -contains $CurrentBranch) {
            [void]$blockers.Add(("Refusing to finish on '{0}': the harness never commits to a base branch." -f $CurrentBranch))
        }
        if (-not $DeclaredBranch) {
            [void]$blockers.Add('The task specification declares no branch.')
        }
        elseif ($CurrentBranch -ne $DeclaredBranch) {
            [void]$blockers.Add(("HEAD is on '{0}' but the task declares '{1}'. Switch to the task branch yourself, or fix the task file." -f $CurrentBranch, $DeclaredBranch))
        }
    }

    return [pscustomobject]@{
        IsOnTaskBranch = ($blockers.Count -eq 0)
        CurrentBranch  = $CurrentBranch
        DeclaredBranch = $DeclaredBranch
        IsDetached     = $IsDetached
        Blockers       = @($blockers)
    }
}

# ---------------------------------------------------------------------------
# Candidate paths
# ---------------------------------------------------------------------------

function Get-DevFinishCandidatePaths {
    <#
    .SYNOPSIS
        The paths -Finish will scope-check and, if they pass, stage.

    .DESCRIPTION
        Built from Get-DevWorkingTreeChanges, with two corrections that matter:

        1. STAGED PATHS ARE INCLUDED. ChangedPaths is 'git diff --name-only'
           plus untracked files, which omits anything already staged. A
           pre-staged path would otherwise be committed WITHOUT ever passing
           the allowed_paths check -- the union closes that hole, so every path
           in the eventual commit has been scope-checked.

        2. PROTECTED AND SCRATCH PATHS ARE SUBTRACTED EXPLICITLY. Untracked
           data/ files never reach ChangedPaths in the first place (they
           classify as Protected), but a TRACKED file under data/ would appear
           in 'git diff --name-only'. Nothing under data/ is tracked today;
           this does not rely on that staying true.

        Returns the excluded sets as well, so the report can state plainly what
        was left out rather than leaving it invisible.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Changes)

    $union = @(@($Changes.ChangedPaths) + @($Changes.StagedTracked)) |
             Where-Object { $_ -ne $null -and $_ -ne '' } |
             ForEach-Object { ([string]$_).Replace('\', '/') } |
             Sort-Object -Unique

    $candidates = New-Object System.Collections.ArrayList
    $excludedProtected = New-Object System.Collections.ArrayList
    $excludedScratch = New-Object System.Collections.ArrayList

    foreach ($path in $union) {
        $isProtected = $false
        foreach ($prefix in $DevProtectedPathPrefixes) {
            if ($path -eq $prefix.TrimEnd('/') -or $path.StartsWith($prefix)) {
                $isProtected = $true
                break
            }
        }
        if ($isProtected) { [void]$excludedProtected.Add($path); continue }
        if ($DevScratchPaths -contains $path) { [void]$excludedScratch.Add($path); continue }
        [void]$candidates.Add($path)
    }

    $stagedOnly = @(@($Changes.StagedTracked) |
        Where-Object { @($Changes.ChangedPaths) -notcontains $_ })

    return [pscustomobject]@{
        CandidatePaths    = @($candidates)
        ExcludedProtected = @($excludedProtected)
        ExcludedScratch   = @($excludedScratch)
        StagedOnly        = @($stagedOnly)
        HasCandidates     = ($candidates.Count -gt 0)
    }
}

# ---------------------------------------------------------------------------
# Commit message / PR body
# ---------------------------------------------------------------------------

function Get-DevLastClaudeSessionId {
    <#
    .SYNOPSIS
        The session id of the most recent -RunClaude run for this task, or
        $null.

    .DESCRIPTION
        Reads .dev/runs/<TaskId>/<stamp>/run-summary.json, newest first, and
        returns the SessionId of the first one whose Mode is RunClaude.

        Returns $null when there is none. The caller OMITS the Claude-Session
        trailer in that case: a commit trailer that points at a session which
        never existed is worse than no trailer at all, so one is never
        invented.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$TaskId
    )

    $taskRunsDir = Join-Path (Join-Path $RepoRoot '.dev\runs') $TaskId
    if (-not (Test-Path -LiteralPath $taskRunsDir)) { return $null }

    $runDirs = @(Get-ChildItem -LiteralPath $taskRunsDir -Directory -ErrorAction SilentlyContinue |
                 Sort-Object -Property Name -Descending)

    foreach ($dir in $runDirs) {
        $summaryPath = Join-Path $dir.FullName 'run-summary.json'
        if (-not (Test-Path -LiteralPath $summaryPath)) { continue }
        try {
            $summary = Get-Content -LiteralPath $summaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            continue
        }
        if ($summary -and $summary.Mode -eq 'RunClaude' -and $summary.SessionId) {
            return [string]$summary.SessionId
        }
    }
    return $null
}

function New-DevCommitMessage {
    <#
    .SYNOPSIS
        Build the commit message text written to the message FILE.

    .DESCRIPTION
        Written to a file and passed to 'git commit -F' rather than '-m': the
        message contains newlines, quotes and a URL, and PowerShell 5.1's
        native-argument quoting is not reliable enough to be trusted with any
        of them.

        The subject is the task's 'commit_subject' if it declares one, else its
        title. It is never truncated -- an over-long subject is reported as a
        warning so the author can fix the task file, not silently mangled.

        The Claude-Session trailer appears only when a real -RunClaude session
        id was found (see Get-DevLastClaudeSessionId). Co-Authored-By always
        appears: the code in the commit was written by Claude under the
        harness, whichever session produced it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Spec,
        [Parameter(Mandatory = $true)][string]$TaskId,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Paths,
        [string]$TestSummary,
        [string]$ClaudeSessionId
    )

    $subject = [string]$Spec.FrontMatter['title']
    if ($Spec.FrontMatter.ContainsKey('commit_subject') -and $Spec.FrontMatter['commit_subject']) {
        $subject = [string]$Spec.FrontMatter['commit_subject']
    }
    $subject = $subject.Trim()

    $warnings = New-Object System.Collections.ArrayList
    if ($subject.Length -gt 72) {
        [void]$warnings.Add(("Commit subject is {0} characters (>72). Declare a shorter 'commit_subject' in the task file if that matters to you; it is not truncated here." -f $subject.Length))
    }

    $builder = New-Object System.Text.StringBuilder
    [void]$builder.AppendLine($subject)
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine(('Task: {0} ({1})' -f $TaskId, $Spec.RelativePath))
    [void]$builder.AppendLine(('Files changed: {0}' -f @($Paths).Count))
    if ($TestSummary) {
        [void]$builder.AppendLine(('Tests: {0}' -f $TestSummary))
    }
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine($DevCommitCoAuthor)
    if ($ClaudeSessionId) {
        [void]$builder.AppendLine(('Claude-Session: {0}{1}' -f $DevClaudeSessionUrlPrefix, $ClaudeSessionId))
    }

    return [pscustomobject]@{
        Text            = $builder.ToString()
        Subject         = $subject
        HasSessionId    = [bool]$ClaudeSessionId
        ClaudeSessionId = $ClaudeSessionId
        Warnings        = @($warnings)
    }
}

function New-DevPrBody {
    <#
    .SYNOPSIS
        Build the pull-request body written to a file for 'gh pr create
        --body-file'.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Spec,
        [Parameter(Mandatory = $true)][string]$TaskId,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Paths,
        [string]$TestSummary
    )

    $builder = New-Object System.Text.StringBuilder
    [void]$builder.AppendLine(('## {0}' -f $Spec.FrontMatter['title']))
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine(('Task specification: `{0}`' -f $Spec.RelativePath))
    if ($Spec.FrontMatter.ContainsKey('module')) {
        [void]$builder.AppendLine(('Module: {0}' -f $Spec.FrontMatter['module']))
    }
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine('### Files changed')
    [void]$builder.AppendLine('')
    foreach ($path in @($Paths)) {
        [void]$builder.AppendLine(('- `{0}`' -f $path))
    }
    [void]$builder.AppendLine('')
    if ($TestSummary) {
        [void]$builder.AppendLine('### Tests')
        [void]$builder.AppendLine('')
        [void]$builder.AppendLine(('`{0}` -- {1}' -f $Spec.FrontMatter['test_command'], $TestSummary))
        [void]$builder.AppendLine('')
    }
    [void]$builder.AppendLine('---')
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine('Prepared by the Oscill8 development harness (`dev.ps1 -Finish`). The diff was')
    [void]$builder.AppendLine('reviewed by a human before this branch was committed and pushed; merging is a')
    [void]$builder.AppendLine('separate human decision and is never automated.')

    return $builder.ToString()
}

# ---------------------------------------------------------------------------
# Mutating git operations -- one function each, fixed argument vectors
# ---------------------------------------------------------------------------

function Test-DevStageablePath {
    <#
    .SYNOPSIS
        Independent re-validation of one path before it may be staged.

    .DESCRIPTION
        The caller has already scope-checked this path against the task's
        allowed_paths. This is the SECOND, independent filter, written at a
        different layer and deliberately not sharing that one's logic: it
        refuses anything that is not a plain, repository-relative, non-magic
        path to a non-protected file.

    .OUTPUTS
        PSCustomObject: IsValid, Reason, Path (normalised).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Path)

    $reject = {
        param([string]$Reason)
        return [pscustomobject]@{ IsValid = $false; Reason = $Reason; Path = $Path }
    }

    if (-not $Path -or -not $Path.Trim()) {
        return (& $reject 'empty path')
    }

    $normalised = $Path.Trim().Replace('\', '/')

    if ($normalised.StartsWith('-')) {
        return (& $reject "starts with '-' and could be read as an option")
    }
    if ($normalised -eq '.' -or $normalised -eq './' -or $normalised -eq '..') {
        return (& $reject 'refers to a directory tree rather than a file')
    }
    if ($normalised.StartsWith('/') -or $normalised -match '^[A-Za-z]:') {
        return (& $reject 'is absolute; only repository-relative paths may be staged')
    }
    if ($normalised -split '/' -contains '..') {
        return (& $reject "contains a '..' traversal segment")
    }
    foreach ($char in $DevPathSpecForbiddenChars) {
        if ($normalised.Contains($char)) {
            return (& $reject ("contains the forbidden pathspec character '{0}'" -f $char))
        }
    }
    foreach ($prefix in $DevProtectedPathPrefixes) {
        if ($normalised -eq $prefix.TrimEnd('/') -or $normalised.StartsWith($prefix)) {
            return (& $reject ("is under the protected prefix '{0}'" -f $prefix))
        }
    }
    if ($DevScratchPaths -contains $normalised) {
        return (& $reject 'is a declared scratch path')
    }

    return [pscustomobject]@{ IsValid = $true; Reason = $null; Path = $normalised }
}

function Invoke-DevGitAddPaths {
    <#
    .SYNOPSIS
        Stage an EXPLICIT list of paths. The harness's only 'git add'.

    .DESCRIPTION
        Runs exactly:

            git add -- <path1> <path2> ...

        The '--' terminates option parsing, so no element can be interpreted as
        a flag whatever it contains; every element is additionally validated by
        Test-DevStageablePath first, and the whole call is refused if ANY path
        fails (never a partial stage of the acceptable subset -- a caller with
        one bad path has a problem worth stopping for).

        There is no parameter that can produce 'git add .', '-A', '-u', a
        wildcard or a directory sweep. The vector is built here from a
        validated list and nowhere else.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Paths
    )

    $validated = New-Object System.Collections.ArrayList
    $rejections = New-Object System.Collections.ArrayList

    foreach ($path in @($Paths)) {
        $check = Test-DevStageablePath -Path $path
        if ($check.IsValid) { [void]$validated.Add($check.Path) }
        else { [void]$rejections.Add(("'{0}' {1}" -f $path, $check.Reason)) }
    }

    if ($rejections.Count -gt 0) {
        throw ('Invoke-DevGitAddPaths: refusing to stage -- ' + ($rejections -join '; ') + '.')
    }
    if ($validated.Count -eq 0) {
        throw 'Invoke-DevGitAddPaths: refusing to stage an empty path list.'
    }

    $arguments = @('add', '--') + @($validated)

    Push-Location -LiteralPath $RepoRoot
    try {
        $output = & git @arguments
        $code = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    return [pscustomobject]@{
        ExitCode    = $code
        Output      = @($output)
        Command     = 'git add -- ' + ($validated -join ' ')
        StagedPaths = @($validated)
    }
}

function Invoke-DevGitCommit {
    <#
    .SYNOPSIS
        Commit the staged index from a message file. The harness's only
        'git commit'.

    .DESCRIPTION
        Runs exactly:

            git commit -F <MessageFile>

        No -m (quoting), no -a (which would sweep unstaged tracked changes past
        the scope check), no --amend (history rewriting), no --no-verify (hooks
        are the user's business), no --allow-empty. If the index is empty, git
        fails and so does this -- which is the intended behaviour, not an edge
        case to paper over.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$MessageFile
    )

    if (-not (Test-Path -LiteralPath $MessageFile)) {
        throw ("Invoke-DevGitCommit: commit message file not found: '{0}'." -f $MessageFile)
    }

    $arguments = @('commit', '-F', $MessageFile)

    Push-Location -LiteralPath $RepoRoot
    try {
        $output = & git @arguments
        $code = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    $sha = $null
    if ($code -eq 0) {
        Push-Location -LiteralPath $RepoRoot
        try {
            $shaOutput = & git 'rev-parse' 'HEAD'
            if ($LASTEXITCODE -eq 0 -and $shaOutput) { $sha = ([string]@($shaOutput)[0]).Trim() }
        }
        finally {
            Pop-Location
        }
    }

    return [pscustomobject]@{
        ExitCode    = $code
        Output      = @($output)
        Command     = 'git commit -F <message-file>'
        MessageFile = $MessageFile
        CommitSha   = $sha
        Committed   = ($code -eq 0)
    }
}

function Invoke-DevGitPush {
    <#
    .SYNOPSIS
        Push the task branch to origin. The harness's only 'git push'.

    .DESCRIPTION
        Runs exactly:

            git push -u origin <BranchName>

        No --force, no --force-with-lease, no --delete, no --tags, no arbitrary
        refspec: the branch name is re-validated against $DevBranchNamePattern
        and is the only thing that varies. A diverged remote therefore fails
        the push, which is the correct outcome -- the harness never resolves a
        divergence on the user's behalf.

        CREDENTIAL PROMPTS CANNOT HANG THE RUN. GIT_TERMINAL_PROMPT=0 covers
        terminal prompts; the configured credential helper is GUI-based, so
        GCM_INTERACTIVE=Never and GIT_ASKPASS are set as well, and a wall-clock
        timeout terminates the process regardless. All three variables are
        restored afterwards.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$BranchName,
        [Parameter(Mandatory = $true)][string]$StdOutPath,
        [Parameter(Mandatory = $true)][string]$StdErrPath,
        [int]$TimeoutSeconds = 0
    )

    if ($BranchName -notmatch $DevBranchNamePattern) {
        throw ("Invoke-DevGitPush: refusing to push '{0}' -- name does not match {1}." -f $BranchName, $DevBranchNamePattern)
    }
    if ($DevProtectedBranches -contains $BranchName) {
        throw ("Invoke-DevGitPush: refusing to push the base branch '{0}'." -f $BranchName)
    }

    $effectiveTimeout = $DevGitPushTimeoutSeconds
    if ($TimeoutSeconds -gt 0) { $effectiveTimeout = $TimeoutSeconds }

    $arguments = @('push', '-u', 'origin', $BranchName)

    $guards = @{
        'GIT_TERMINAL_PROMPT' = '0'
        'GCM_INTERACTIVE'     = 'Never'
        'GIT_ASKPASS'         = 'echo'
    }
    $saved = @{}
    $had = @{}
    foreach ($name in $guards.Keys) {
        $envPath = 'Env:' + $name
        $had[$name] = Test-Path -LiteralPath $envPath
        if ($had[$name]) { $saved[$name] = (Get-Item -LiteralPath $envPath).Value }
    }

    $startedUtc = (Get-Date).ToUniversalTime()
    $exitCode = $null
    $timedOut = $false
    $launchError = $null

    try {
        foreach ($name in $guards.Keys) {
            Set-Item -LiteralPath ('Env:' + $name) -Value $guards[$name]
        }

        $process = Start-Process -FilePath 'git' `
                                 -ArgumentList $arguments `
                                 -WorkingDirectory $RepoRoot `
                                 -NoNewWindow `
                                 -PassThru `
                                 -RedirectStandardOutput $StdOutPath `
                                 -RedirectStandardError $StdErrPath

        try {
            Wait-Process -InputObject $process -Timeout $effectiveTimeout -ErrorAction Stop
            $exitCode = $process.ExitCode
        }
        catch {
            $timedOut = $true
            try { Stop-Process -InputObject $process -Force -ErrorAction Stop }
            catch { }
        }
    }
    catch {
        $launchError = $_.Exception.Message
    }
    finally {
        foreach ($name in $guards.Keys) {
            if ($had[$name]) { Set-Item -LiteralPath ('Env:' + $name) -Value $saved[$name] }
            else { Remove-Item -LiteralPath ('Env:' + $name) -ErrorAction SilentlyContinue }
        }
    }

    $finishedUtc = (Get-Date).ToUniversalTime()

    return [pscustomobject]@{
        ExitCode        = $exitCode
        TimedOut        = $timedOut
        LaunchError     = $launchError
        Command         = 'git push -u origin ' + $BranchName
        BranchName      = $BranchName
        StdOutPath      = $StdOutPath
        StdErrPath      = $StdErrPath
        TimeoutSeconds  = $effectiveTimeout
        DurationSeconds = [math]::Round(($finishedUtc - $startedUtc).TotalSeconds, 1)
        Pushed          = ($exitCode -eq 0 -and -not $timedOut -and -not $launchError)
    }
}

# ---------------------------------------------------------------------------
# GitHub CLI (optional)
# ---------------------------------------------------------------------------

function Resolve-DevGhExe {
    <#
    .SYNOPSIS
        Locate the GitHub CLI, or report that it is absent.

    .DESCRIPTION
        Absence is NOT an error at this layer and is not an error at any layer
        above it: -Finish falls back to printing a compare URL. This function
        only reports what it found.

        Honours the $DevGhExeOverrideVar environment variable as a test seam so
        the pull-request paths can be exercised against a stub.
    #>
    [CmdletBinding()]
    param()

    $tried = New-Object System.Collections.ArrayList

    $override = [Environment]::GetEnvironmentVariable($DevGhExeOverrideVar)
    if ($override) {
        [void]$tried.Add('$env:' + $DevGhExeOverrideVar + ' = ' + $override)
        if (Test-Path -LiteralPath $override) {
            return [pscustomobject]@{
                Resolved = $true; Path = $override; Source = 'environment override'
                Tried = @($tried); Error = $null
            }
        }
    }

    [void]$tried.Add('Get-Command gh')
    $command = Get-Command 'gh' -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return [pscustomobject]@{
            Resolved = $true; Path = $command.Source; Source = 'PATH'
            Tried = @($tried); Error = $null
        }
    }

    foreach ($candidate in $DevGhCandidatePaths) {
        if (-not $candidate) { continue }
        [void]$tried.Add($candidate)
        if (Test-Path -LiteralPath $candidate) {
            return [pscustomobject]@{
                Resolved = $true; Path = $candidate; Source = 'known install location'
                Tried = @($tried); Error = $null
            }
        }
    }

    return [pscustomobject]@{
        Resolved = $false
        Path     = $null
        Source   = $null
        Tried    = @($tried)
        Error    = 'GitHub CLI (gh) was not found. This is not a failure -- a compare URL is reported instead.'
    }
}

function Test-DevGhAuthenticated {
    <#
    .SYNOPSIS
        Is this gh signed in? Runs 'gh auth status' with prompting disabled.

    .DESCRIPTION
        A signed-out gh is treated exactly like an absent one: the fallback
        path, not an error. Never prints or returns token material -- only the
        exit code and whether it succeeded.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$GhExe,
        [int]$TimeoutSeconds = 60
    )

    $stdOut = [System.IO.Path]::GetTempFileName()
    $stdErr = [System.IO.Path]::GetTempFileName()

    $hadPrompt = Test-Path -LiteralPath 'Env:GH_PROMPT_DISABLED'
    $savedPrompt = $null
    if ($hadPrompt) { $savedPrompt = $env:GH_PROMPT_DISABLED }

    $exitCode = $null
    $timedOut = $false
    $launchError = $null
    try {
        $env:GH_PROMPT_DISABLED = '1'
        $process = Start-Process -FilePath $GhExe `
                                 -ArgumentList @('auth', 'status') `
                                 -NoNewWindow -PassThru `
                                 -RedirectStandardOutput $stdOut `
                                 -RedirectStandardError $stdErr
        try {
            Wait-Process -InputObject $process -Timeout $TimeoutSeconds -ErrorAction Stop
            $exitCode = $process.ExitCode
        }
        catch {
            $timedOut = $true
            try { Stop-Process -InputObject $process -Force -ErrorAction Stop } catch { }
        }
    }
    catch {
        $launchError = $_.Exception.Message
    }
    finally {
        if ($hadPrompt) { $env:GH_PROMPT_DISABLED = $savedPrompt }
        else { Remove-Item -LiteralPath 'Env:GH_PROMPT_DISABLED' -ErrorAction SilentlyContinue }
    }

    Remove-Item -LiteralPath $stdOut -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stdErr -ErrorAction SilentlyContinue

    $authenticated = ($exitCode -eq 0 -and -not $timedOut -and -not $launchError)
    $message = 'gh is authenticated.'
    if (-not $authenticated) {
        $message = 'gh is present but not authenticated (or did not respond); falling back to a compare URL.'
    }

    return [pscustomobject]@{
        Authenticated = $authenticated
        ExitCode      = $exitCode
        TimedOut      = $timedOut
        LaunchError   = $launchError
        Message       = $message
    }
}

function Get-DevCompareUrl {
    <#
    .SYNOPSIS
        The GitHub compare URL for this branch, derived from origin.

    .DESCRIPTION
        Handles both remote forms ('https://github.com/owner/repo.git' and
        'git@github.com:owner/repo.git'). Returns $null for a non-GitHub or
        unparseable remote rather than guessing -- the report then simply says
        no compare URL could be derived.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][AllowNull()][string]$RemoteUrl,
        [Parameter(Mandatory = $true)][string]$BranchName,
        [string]$BaseBranch = 'main'
    )

    if (-not $RemoteUrl) { return $null }

    $url = $RemoteUrl.Trim()
    $slug = $null

    if ($url -match '^https?://[^/]*github\.com/(.+?)(?:\.git)?/?$') {
        $slug = $Matches[1]
    }
    elseif ($url -match '^(?:ssh://)?git@github\.com[:/](.+?)(?:\.git)?/?$') {
        $slug = $Matches[1]
    }

    if (-not $slug) { return $null }

    return ('https://github.com/{0}/compare/{1}...{2}?expand=1' -f $slug, $BaseBranch, $BranchName)
}

function New-DevPullRequest {
    <#
    .SYNOPSIS
        Open a pull request with the GitHub CLI, if one is usable.

    .DESCRIPTION
        Runs exactly:

            gh pr create --base <BaseBranch> --head <BranchName>
                         --title <Title> --body-file <BodyFile>

        No --fill (which would invent a title/body from commits), no --merge,
        no --auto, no --draft toggle, and nothing that merges: opening the PR
        is where the harness stops and the human takes over.

        Three outcomes, and the caller maps them to exit codes:
            Attempted = false            gh missing or signed out  -> success
            Attempted, Created = true    PR opened                 -> success
            Attempted, Created = false   gh failed                 -> PrFailed
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$BranchName,
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$BodyFile,
        [Parameter(Mandatory = $true)][string]$StdOutPath,
        [Parameter(Mandatory = $true)][string]$StdErrPath,
        [string]$BaseBranch = 'main',
        [string]$GhExe,
        [int]$TimeoutSeconds = 180
    )

    if ($BranchName -notmatch $DevBranchNamePattern) {
        throw ("New-DevPullRequest: refusing to open a PR for '{0}' -- name does not match {1}." -f $BranchName, $DevBranchNamePattern)
    }
    if (-not (Test-Path -LiteralPath $BodyFile)) {
        throw ("New-DevPullRequest: PR body file not found: '{0}'." -f $BodyFile)
    }

    $arguments = @(
        'pr', 'create',
        '--base', $BaseBranch,
        '--head', $BranchName,
        '--title', $Title,
        '--body-file', $BodyFile
    )

    $exitCode = $null
    $timedOut = $false
    $launchError = $null

    try {
        $process = Start-Process -FilePath $GhExe `
                                 -ArgumentList $arguments `
                                 -WorkingDirectory $RepoRoot `
                                 -NoNewWindow -PassThru `
                                 -RedirectStandardOutput $StdOutPath `
                                 -RedirectStandardError $StdErrPath
        try {
            Wait-Process -InputObject $process -Timeout $TimeoutSeconds -ErrorAction Stop
            $exitCode = $process.ExitCode
        }
        catch {
            $timedOut = $true
            try { Stop-Process -InputObject $process -Force -ErrorAction Stop } catch { }
        }
    }
    catch {
        $launchError = $_.Exception.Message
    }

    $prUrl = $null
    if (Test-Path -LiteralPath $StdOutPath) {
        $match = @(Get-Content -LiteralPath $StdOutPath |
                   Where-Object { $_ -match 'https://[^\s]*/pull/\d+' })
        if ($match.Count -gt 0) {
            if ($match[-1] -match '(https://[^\s]*/pull/\d+)') { $prUrl = $Matches[1] }
        }
    }

    return [pscustomobject]@{
        Attempted   = $true
        Created     = ($exitCode -eq 0 -and -not $timedOut -and -not $launchError)
        Url         = $prUrl
        ExitCode    = $exitCode
        TimedOut    = $timedOut
        LaunchError = $launchError
        Command     = ('gh pr create --base {0} --head {1} --title <title> --body-file <file>' -f $BaseBranch, $BranchName)
        BaseBranch  = $BaseBranch
        StdOutPath  = $StdOutPath
        StdErrPath  = $StdErrPath
    }
}
