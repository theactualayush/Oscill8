<#
.SYNOPSIS
    Safe feature-branch creation for the Oscill8 development harness.

.DESCRIPTION
    Dot-source alongside _Common.ps1 and Test-RepoState.ps1:

        . .\scripts\_Common.ps1
        . .\scripts\Test-RepoState.ps1
        . .\scripts\New-TaskBranch.ps1

    This file contains THE ONLY MUTATING GIT CALL IN THE ENTIRE HARNESS:
    Invoke-DevGitCreateBranch, which runs exactly

        git switch -c <validated-branch-name> <validated-base-sha>

    and nothing else. It builds its own argument vector from two arguments
    that are both re-validated against strict patterns inside the function.
    It accepts no caller-supplied argument list, so it cannot be coerced into
    running any other git subcommand.

    Everything else here is read-only and routes through Invoke-DevGit's
    existing read-only allow-list.

    NEVER DONE, ANYWHERE IN THIS FILE:
        add, commit, amend, push, pull, fetch, merge, rebase, cherry-pick,
        clean, stash (in any form), reset, restore, checkout -- <path>,
        history rewriting, or any broad pathspec. There is no code path that
        cleans, stashes, reverts or stages the user's working tree, and no
        flag that enables one.

    The working tree is never modified by branch creation: the branch is
    pinned to the SHA that HEAD is verified to be pointing at immediately
    beforehand, so 'switch' performs no file updates. That immediate
    re-verification is load-bearing, not decorative -- switching to a
    DIFFERENT commit would rewrite tracked files.

.NOTES
    Windows PowerShell 5.1 compatible.
#>

# Branch names accepted by this harness. Also enforced by Read-DevTaskSpec at
# specification-parse time; re-checked here so the mutating call can never be
# reached with an unvalidated name, whatever the caller did.
$DevBranchNamePattern = '^task/TASK-\d{3}-[a-z0-9-]+$'

function Test-DevBranchName {
    <#
    .SYNOPSIS
        Validate a branch name against the harness pattern.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$BranchName)

    return ($BranchName -match $DevBranchNamePattern)
}

function Test-DevWorkingTreeSafeForBranch {
    <#
    .SYNOPSIS
        Decide whether a classified working tree is safe to branch from.

    .DESCRIPTION
        Applies the classification rules already established by
        Get-DevDirtyClassification -- this function adds no new taxonomy:

            Protected        (data/)      ALLOWED. Live user data, invisible
                                          to branch creation, never touched.
            Scratch          (declared)   ALLOWED. Expected to be dirty.
            DirtyTracked                  REFUSED. A tracked modification
                                          would silently ride along onto the
                                          new branch and become
                                          indistinguishable from the task's
                                          own work.
            UnknownUntracked              REFUSED. An undeclared untracked
                                          path would do the same.

        The remedy is always the user's to choose -- commit it, revert it, or
        declare it as scratch. This function never resolves it for them.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$DirtyPaths)

    $blockers = New-Object System.Collections.ArrayList
    $allowed = New-Object System.Collections.ArrayList

    $dirtyTracked = @($DirtyPaths | Where-Object { $_.Classification -eq 'DirtyTracked' })
    $unknownUntracked = @($DirtyPaths | Where-Object { $_.Classification -eq 'UnknownUntracked' })
    $protected = @($DirtyPaths | Where-Object { $_.Classification -eq 'Protected' })
    $scratch = @($DirtyPaths | Where-Object { $_.Classification -eq 'Scratch' })

    foreach ($entry in $dirtyTracked) {
        [void]$blockers.Add(("Tracked file has uncommitted changes: {0}" -f $entry.Path))
    }
    foreach ($entry in $unknownUntracked) {
        [void]$blockers.Add(("Untracked path is neither declared scratch nor protected: {0}" -f $entry.Path))
    }
    foreach ($entry in $protected) {
        [void]$allowed.Add(("Protected (untouched): {0}" -f $entry.Path))
    }
    foreach ($entry in $scratch) {
        [void]$allowed.Add(("Declared scratch (ignored): {0}" -f $entry.Path))
    }

    return [pscustomobject]@{
        IsSafe                = ($blockers.Count -eq 0)
        Blockers              = @($blockers)
        Allowed               = @($allowed)
        DirtyTrackedCount     = $dirtyTracked.Count
        UnknownUntrackedCount = $unknownUntracked.Count
        ProtectedCount        = $protected.Count
        ScratchCount          = $scratch.Count
    }
}

function Test-DevBranchExistsLocal {
    <#
    .SYNOPSIS
        Does refs/heads/<BranchName> already exist locally?

    .DESCRIPTION
        Uses show-ref --verify on the fully-qualified ref, so a partial or
        glob-like name cannot accidentally match a different branch.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BranchName,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    $result = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @(
        'show-ref', '--verify', '--quiet', ('refs/heads/' + $BranchName)
    )
    return ($result.ExitCode -eq 0)
}

function Test-DevBranchExistsRemote {
    <#
    .SYNOPSIS
        Does the branch already exist on origin?

    .DESCRIPTION
        Read-only network call. GIT_TERMINAL_PROMPT=0 is set for the duration
        so an auth challenge fails fast instead of blocking the harness on an
        invisible credential prompt.

        Three distinct outcomes, deliberately not collapsed into a boolean:
          Performed=false           -- no 'origin' remote configured; there is
                                       no remote for the branch to collide
                                       with, so this is not a failure.
          Performed=true,  Reachable=false -- the check could not be completed
                                       (offline, auth). This is treated as a
                                       BLOCKER by the caller: an unverified
                                       precondition is not a satisfied one.
          Performed=true,  Reachable=true  -- Exists is authoritative.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BranchName,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    $originResult = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @('remote')
    $remotes = @($originResult.Output | Where-Object { $_ -ne $null -and $_ -ne '' } |
                 ForEach-Object { ([string]$_).Trim() })
    if ($remotes -notcontains 'origin') {
        return [pscustomobject]@{
            Performed = $false
            Reachable = $false
            Exists    = $false
            Message   = "No 'origin' remote is configured; there is no remote branch to collide with."
        }
    }

    $hadPrompt = Test-Path -LiteralPath 'Env:GIT_TERMINAL_PROMPT'
    $previousPrompt = $env:GIT_TERMINAL_PROMPT
    try {
        $env:GIT_TERMINAL_PROMPT = '0'
        $result = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @(
            'ls-remote', '--heads', 'origin', ('refs/heads/' + $BranchName)
        )
    }
    finally {
        if ($hadPrompt) { $env:GIT_TERMINAL_PROMPT = $previousPrompt }
        else { Remove-Item -LiteralPath 'Env:GIT_TERMINAL_PROMPT' -ErrorAction SilentlyContinue }
    }

    if ($result.ExitCode -ne 0) {
        return [pscustomobject]@{
            Performed = $true
            Reachable = $false
            Exists    = $false
            Message   = ("git ls-remote failed with exit code {0}; the remote branch check could not be completed." -f $result.ExitCode)
        }
    }

    $matchingRefs = @($result.Output | Where-Object { $_ -ne $null -and $_ -ne '' })
    return [pscustomobject]@{
        Performed = $true
        Reachable = $true
        Exists    = ($matchingRefs.Count -gt 0)
        Message   = ("ls-remote returned {0} matching ref(s)." -f $matchingRefs.Count)
    }
}

function Invoke-DevGitCreateBranch {
    <#
    .SYNOPSIS
        THE ONLY MUTATING GIT CALL IN THIS HARNESS.

    .DESCRIPTION
        Runs exactly:

            git switch -c <BranchName> <BaseSha>

        Both arguments are re-validated here against strict patterns before
        the argument vector is built. The vector is constructed inside this
        function; there is no parameter through which a caller could append,
        replace or inject another argument or subcommand.

        The explicit <BaseSha> start point closes a time-of-check /
        time-of-use window: without it, 'switch -c' would silently branch
        from wherever HEAD happened to be at that instant. The caller must
        have verified HEAD == BaseSha immediately before calling, which also
        guarantees this switch updates no files in the working tree.

        Deliberately NOT routed through Invoke-DevGit, whose allow-list stays
        read-only so that no other caller can ever reach a mutating
        subcommand through the general-purpose wrapper.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$BranchName,
        [Parameter(Mandatory = $true)][string]$BaseSha
    )

    if ($BranchName -notmatch $DevBranchNamePattern) {
        throw ("Invoke-DevGitCreateBranch: refusing to create branch '{0}' -- name does not match {1}." -f $BranchName, $DevBranchNamePattern)
    }
    if ($BaseSha -notmatch '^[0-9a-f]{40}$') {
        throw ("Invoke-DevGitCreateBranch: refusing to create a branch from '{0}' -- not a full 40-character SHA." -f $BaseSha)
    }

    $arguments = @('switch', '-c', $BranchName, $BaseSha)

    Push-Location -LiteralPath $RepoRoot
    try {
        $output = & git @arguments
        $code = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    return [pscustomobject]@{
        ExitCode = $code
        Output   = @($output)
        Command  = 'git ' + ($arguments -join ' ')
    }
}

function New-DevTaskBranch {
    <#
    .SYNOPSIS
        Run every branch precondition, then create the task branch.

    .DESCRIPTION
        Order of operations, all-or-nothing -- the mutating call is reached
        only if every preceding check passed:

            1. Branch name matches the required pattern.
            2. HEAD is not detached.
            3. HEAD is on the expected base branch (main).
            4. HEAD's SHA still equals the recorded base SHA (TOCTOU guard).
            5. Branch does not exist locally.
            6. Branch does not exist on origin (or origin is absent).
            7. git switch -c <name> <base-sha>
            8. HEAD is now the new branch, still at the base SHA.

        A failure at any step returns Created=$false with the reason. The
        working tree is never modified, cleaned, stashed or staged on any
        path through this function, including the failure paths.

    .OUTPUTS
        PSCustomObject describing every check, the git commands executed and
        the post-creation verification.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$BranchName,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$BaseSha,
        [string]$ExpectedBaseBranch = 'main'
    )

    $blockers = New-Object System.Collections.ArrayList
    $warnings = New-Object System.Collections.ArrayList
    $gitCommands = New-Object System.Collections.ArrayList
    $checks = [ordered]@{}

    # --- 1. name pattern ---------------------------------------------------
    $checks['NameMatchesPattern'] = (Test-DevBranchName -BranchName $BranchName)
    if (-not $checks['NameMatchesPattern']) {
        [void]$blockers.Add(("Branch name '{0}' does not match {1}." -f $BranchName, $DevBranchNamePattern))
    }

    # --- 2/3. HEAD identity, re-read now rather than trusting preflight ----
    $headBranch = $null
    $headResult = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @('rev-parse', '--abbrev-ref', 'HEAD')
    [void]$gitCommands.Add($headResult.Command)
    if ($headResult.ExitCode -eq 0 -and $headResult.Output.Count -gt 0) {
        $headBranch = ([string]$headResult.Output[0]).Trim()
    }

    $checks['HeadNotDetached'] = ($headBranch -and $headBranch -ne 'HEAD')
    if (-not $checks['HeadNotDetached']) {
        [void]$blockers.Add('HEAD is detached; refusing to create a branch.')
    }

    $checks['HeadOnBaseBranch'] = ($headBranch -eq $ExpectedBaseBranch)
    if (-not $checks['HeadOnBaseBranch']) {
        [void]$blockers.Add(("HEAD is on '{0}', not the expected base branch '{1}'." -f $headBranch, $ExpectedBaseBranch))
    }

    # --- 4. TOCTOU guard ---------------------------------------------------
    $headSha = $null
    $shaResult = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @('rev-parse', 'HEAD')
    [void]$gitCommands.Add($shaResult.Command)
    if ($shaResult.ExitCode -eq 0 -and $shaResult.Output.Count -gt 0) {
        $headSha = ([string]$shaResult.Output[0]).Trim()
    }

    $checks['HeadShaMatchesBaseSha'] = ($headSha -and $BaseSha -and $headSha -eq $BaseSha)
    if (-not $checks['HeadShaMatchesBaseSha']) {
        [void]$blockers.Add(("HEAD is at '{0}' but the recorded base SHA is '{1}'; refusing to branch from a moving target." -f $headSha, $BaseSha))
    }

    # --- 5. local collision ------------------------------------------------
    $existsLocal = $false
    if ($checks['NameMatchesPattern']) {
        $existsLocal = Test-DevBranchExistsLocal -BranchName $BranchName -RepoRoot $RepoRoot
        [void]$gitCommands.Add('git show-ref --verify --quiet refs/heads/' + $BranchName)
    }
    $checks['NotExistsLocally'] = (-not $existsLocal)
    if ($existsLocal) {
        [void]$blockers.Add(("Branch '{0}' already exists locally." -f $BranchName))
    }

    # --- 6. remote collision -----------------------------------------------
    $remoteCheck = $null
    if ($checks['NameMatchesPattern']) {
        $remoteCheck = Test-DevBranchExistsRemote -BranchName $BranchName -RepoRoot $RepoRoot
        if ($remoteCheck.Performed) {
            [void]$gitCommands.Add('git ls-remote --heads origin refs/heads/' + $BranchName)
        }
    }

    $checks['NotExistsOnOrigin'] = $false
    if ($remoteCheck) {
        if (-not $remoteCheck.Performed) {
            $checks['NotExistsOnOrigin'] = $true
            [void]$warnings.Add($remoteCheck.Message)
        }
        elseif (-not $remoteCheck.Reachable) {
            [void]$blockers.Add(("Remote branch check could not be completed: {0}" -f $remoteCheck.Message))
        }
        elseif ($remoteCheck.Exists) {
            [void]$blockers.Add(("Branch '{0}' already exists on origin." -f $BranchName))
        }
        else {
            $checks['NotExistsOnOrigin'] = $true
        }
    }

    # --- 7. the single mutating call ---------------------------------------
    $created = $false
    $createResult = $null
    if ($blockers.Count -eq 0) {
        $createResult = Invoke-DevGitCreateBranch -RepoRoot $RepoRoot -BranchName $BranchName -BaseSha $BaseSha
        [void]$gitCommands.Add($createResult.Command)
        if ($createResult.ExitCode -ne 0) {
            [void]$blockers.Add(("git switch -c failed with exit code {0}." -f $createResult.ExitCode))
        }
        else {
            $created = $true
        }
    }

    # --- 8. post-creation verification -------------------------------------
    $postBranch = $null
    $postSha = $null
    $postVerified = $false
    if ($created) {
        $postBranchResult = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @('rev-parse', '--abbrev-ref', 'HEAD')
        [void]$gitCommands.Add($postBranchResult.Command)
        if ($postBranchResult.ExitCode -eq 0 -and $postBranchResult.Output.Count -gt 0) {
            $postBranch = ([string]$postBranchResult.Output[0]).Trim()
        }

        $postShaResult = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @('rev-parse', 'HEAD')
        [void]$gitCommands.Add($postShaResult.Command)
        if ($postShaResult.ExitCode -eq 0 -and $postShaResult.Output.Count -gt 0) {
            $postSha = ([string]$postShaResult.Output[0]).Trim()
        }

        $postVerified = (($postBranch -eq $BranchName) -and ($postSha -eq $BaseSha))
        if (-not $postVerified) {
            [void]$blockers.Add(("Post-creation verification failed: HEAD is '{0}' at '{1}', expected '{2}' at '{3}'." -f $postBranch, $postSha, $BranchName, $BaseSha))
        }
    }
    $checks['PostCreationVerified'] = $postVerified

    return [pscustomobject]@{
        BranchName     = $BranchName
        BaseSha        = $BaseSha
        BaseBranch     = $ExpectedBaseBranch
        Created        = ($created -and $postVerified)
        SwitchExitCode = $(if ($createResult) { $createResult.ExitCode } else { $null })
        Checks         = $checks
        RemoteCheck    = $remoteCheck
        PostBranch     = $postBranch
        PostSha        = $postSha
        GitCommands    = @($gitCommands)
        Blockers       = @($blockers)
        Warnings       = @($warnings)
    }
}
