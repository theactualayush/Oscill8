<#
.SYNOPSIS
    Read-only preflight inspection of the Oscill8 repository's state.

.DESCRIPTION
    Dot-source alongside _Common.ps1, then call Test-DevRepoState:

        . .\scripts\_Common.ps1
        . .\scripts\Test-RepoState.ps1
        Test-DevRepoState -RepoRoot "C:\path\to\Oscill8_New"

    Every Git call routes through Invoke-DevGit's read-only allow-list. This
    stage NEVER creates a branch, stages, stashes, cleans, resets or restores.

    A dirty working tree is CLASSIFIED and REPORTED, never blocked on and
    never "fixed" -- in the dry-run layer the current tree IS the baseline
    observation state.

.NOTES
    Windows PowerShell 5.1 compatible.
#>

function Get-DevDirtyClassification {
    <#
    .SYNOPSIS
        Bucket each 'git status --porcelain' line into one of four classes.

    .DESCRIPTION
        Protected        -- under data/. Live user data: never staged,
                            stashed, cleaned or modified by this harness.
        Scratch          -- declared in $DevScratchPaths. Expected to be
                            dirty; carries no signal.
        DirtyTracked     -- a tracked file with uncommitted modifications.
        UnknownUntracked -- an untracked path the harness has no story for.

        Classification is the whole job. Nothing here acts on the result.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$StatusLines
    )

    $classified = New-Object System.Collections.ArrayList

    foreach ($line in $StatusLines) {
        if (-not $line) { continue }
        if ($line.Length -lt 4) { continue }

        $indexStatus = $line.Substring(0, 1)
        $workTreeStatus = $line.Substring(1, 1)
        $pathPart = $line.Substring(3).Trim()

        # Rename/copy entries read 'old -> new'; the destination is the path
        # that actually exists in the working tree.
        if ($pathPart -match '^(.*)\s->\s(.*)$') {
            $pathPart = $Matches[2]
        }
        $path = $pathPart.Trim('"').Replace('\', '/')

        $isUntracked = ($indexStatus -eq '?' -and $workTreeStatus -eq '?')

        $class = 'DirtyTracked'
        $reason = 'Tracked file with uncommitted changes.'

        $isProtected = $false
        foreach ($prefix in $DevProtectedPathPrefixes) {
            if ($path -eq $prefix.TrimEnd('/') -or $path.StartsWith($prefix)) {
                $isProtected = $true
                break
            }
        }

        if ($isProtected) {
            $class = 'Protected'
            $reason = 'Live user data under a protected prefix; read-only to this harness.'
        }
        elseif ($DevScratchPaths -contains $path) {
            $class = 'Scratch'
            $reason = 'Declared scratch/scaffolding path; expected to be dirty.'
        }
        elseif ($isUntracked) {
            $class = 'UnknownUntracked'
            $reason = 'Untracked path not declared as scratch or protected.'
        }

        [void]$classified.Add([pscustomobject]@{
            Path           = $path
            StatusCode     = $line.Substring(0, 2)
            IsUntracked    = $isUntracked
            Classification = $class
            Reason         = $reason
        })
    }

    return @($classified)
}

function Test-DevRepoState {
    <#
    .SYNOPSIS
        Gather repository, branch, working-tree and toolchain facts.

    .OUTPUTS
        PSCustomObject. 'Blockers' is non-empty when the harness must stop;
        'Warnings' is advisory only and never halts a dry run.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [string]$ExpectedBaseBranch = 'main'
    )

    $blockers = New-Object System.Collections.ArrayList
    $warnings = New-Object System.Collections.ArrayList

    # --- git repository identity ------------------------------------------
    $topLevel = $null
    $insideRepo = $false
    try {
        $result = Invoke-DevGit -Arguments @('rev-parse', '--show-toplevel') -RepoRoot $RepoRoot
        if ($result.ExitCode -eq 0 -and $result.Output.Count -gt 0) {
            $insideRepo = $true
            $topLevel = ([string]$result.Output[0]).Trim()
        }
    }
    catch {
        [void]$blockers.Add("Unable to run git: $($_.Exception.Message)")
    }

    if (-not $insideRepo) {
        [void]$blockers.Add('Not inside a Git repository (git rev-parse --show-toplevel failed).')
    }
    else {
        $normalisedTop = $topLevel.Replace('/', '\').TrimEnd('\')
        $normalisedRoot = ([System.IO.Path]::GetFullPath($RepoRoot)).TrimEnd('\')
        if ($normalisedTop -ne $normalisedRoot) {
            [void]$warnings.Add("git top-level '$topLevel' differs from the resolved repository root '$RepoRoot'.")
        }
    }

    # --- HEAD / branch -----------------------------------------------------
    $currentBranch = $null
    $isDetached = $false
    if ($insideRepo) {
        $branchResult = Invoke-DevGit -Arguments @('rev-parse', '--abbrev-ref', 'HEAD') -RepoRoot $RepoRoot
        if ($branchResult.ExitCode -eq 0 -and $branchResult.Output.Count -gt 0) {
            $currentBranch = ([string]$branchResult.Output[0]).Trim()
        }
        if ($currentBranch -eq 'HEAD' -or -not $currentBranch) {
            $isDetached = $true
            [void]$blockers.Add('HEAD is detached; refusing to operate on a detached HEAD.')
        }
        elseif ($currentBranch -ne $ExpectedBaseBranch) {
            [void]$warnings.Add("Current branch is '$currentBranch', not the expected base branch '$ExpectedBaseBranch'.")
        }
    }

    # --- base commit -------------------------------------------------------
    $baseSha = $null
    $baseShaShort = $null
    $baseSubject = $null
    if ($insideRepo -and -not $isDetached) {
        $shaResult = Invoke-DevGit -Arguments @('rev-parse', 'HEAD') -RepoRoot $RepoRoot
        if ($shaResult.ExitCode -eq 0 -and $shaResult.Output.Count -gt 0) {
            $baseSha = ([string]$shaResult.Output[0]).Trim()
            if ($baseSha.Length -ge 7) { $baseShaShort = $baseSha.Substring(0, 7) }
        }
        $subjectResult = Invoke-DevGit -Arguments @('log', '-1', '--pretty=%s') -RepoRoot $RepoRoot
        if ($subjectResult.ExitCode -eq 0 -and $subjectResult.Output.Count -gt 0) {
            $baseSubject = ([string]$subjectResult.Output[0]).Trim()
        }
    }

    # --- remote ------------------------------------------------------------
    $remoteUrl = $null
    if ($insideRepo) {
        $remoteResult = Invoke-DevGit -Arguments @('config', '--get', 'remote.origin.url') -RepoRoot $RepoRoot
        if ($remoteResult.ExitCode -eq 0 -and $remoteResult.Output.Count -gt 0) {
            $remoteUrl = ([string]$remoteResult.Output[0]).Trim()
        }
    }

    # --- working tree ------------------------------------------------------
    $statusLines = @()
    if ($insideRepo) {
        $statusResult = Invoke-DevGit -Arguments @('status', '--porcelain') -RepoRoot $RepoRoot
        if ($statusResult.ExitCode -eq 0) {
            $statusLines = @($statusResult.Output | Where-Object { $_ -ne $null -and $_ -ne '' })
        }
        else {
            [void]$blockers.Add('git status --porcelain failed.')
        }
    }

    $dirtyPaths = Get-DevDirtyClassification -StatusLines $statusLines

    $dirtyTracked = @($dirtyPaths | Where-Object { $_.Classification -eq 'DirtyTracked' })
    $unknownUntracked = @($dirtyPaths | Where-Object { $_.Classification -eq 'UnknownUntracked' })
    if ($dirtyTracked.Count -gt 0) {
        [void]$warnings.Add("$($dirtyTracked.Count) tracked file(s) have uncommitted changes; recorded as pre-existing baseline state.")
    }
    if ($unknownUntracked.Count -gt 0) {
        [void]$warnings.Add("$($unknownUntracked.Count) untracked path(s) are not declared as scratch or protected.")
    }

    # --- is .dev/ ignored? -------------------------------------------------
    $devIgnored = $false
    if ($insideRepo) {
        $ignoreResult = Invoke-DevGit -Arguments @('check-ignore', '-q', '.dev/') -RepoRoot $RepoRoot
        $devIgnored = ($ignoreResult.ExitCode -eq 0)
        if (-not $devIgnored) {
            [void]$warnings.Add('.dev/ is not covered by .gitignore; run artifacts would show up in git status.')
        }
    }

    # --- toolchain ---------------------------------------------------------
    $pythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $pythonExists = Test-Path -LiteralPath $pythonExe
    $pythonVersion = $null
    if (-not $pythonExists) {
        [void]$blockers.Add("Repository virtual environment interpreter not found at '$pythonExe'.")
    }
    else {
        $versionOutput = & $pythonExe '--version'
        if ($LASTEXITCODE -eq 0 -and $versionOutput) {
            $pythonVersion = ([string]$versionOutput).Trim()
        }
        else {
            [void]$blockers.Add("Failed to execute '$pythonExe --version'.")
        }
    }

    $testsDir = Join-Path $RepoRoot 'tests'
    $testsDirExists = Test-Path -LiteralPath $testsDir
    if (-not $testsDirExists) {
        [void]$blockers.Add("tests/ directory not found at '$testsDir'.")
    }

    return [pscustomobject]@{
        RepoRoot         = $RepoRoot
        GitTopLevel      = $topLevel
        IsGitRepository  = $insideRepo
        CurrentBranch    = $currentBranch
        IsDetachedHead   = $isDetached
        BaseSha          = $baseSha
        BaseShaShort     = $baseShaShort
        BaseSubject      = $baseSubject
        RemoteUrl        = $remoteUrl
        StatusLines      = @($statusLines)
        DirtyPaths       = @($dirtyPaths)
        DevDirIgnored    = $devIgnored
        PythonExe        = $pythonExe
        PythonExists     = $pythonExists
        PythonVersion    = $pythonVersion
        TestsDirExists   = $testsDirExists
        Blockers         = @($blockers)
        Warnings         = @($warnings)
        IsUsable         = ($blockers.Count -eq 0)
    }
}
