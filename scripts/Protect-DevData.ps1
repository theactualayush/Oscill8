<#
.SYNOPSIS
    Integrity protection for data/strategy_sets/ and the throwaway sandbox
    used to keep the test suite away from real data.

.DESCRIPTION
    Dot-source alongside _Common.ps1:

        . .\scripts\_Common.ps1
        . .\scripts\Protect-DevData.ps1

    data/strategy_sets/ holds hand-built Strategy Set JSON files that are
    untracked AND (before this harness) not covered by .gitignore -- i.e.
    invisible to Git's protection but fully exposed to Git's destruction.
    They have no version history to recover from, so this file provides:

        New-DevDataSnapshot     SHA-256 + size + mtime manifest (read-only)
        Compare-DevDataSnapshot before/after diff -> Unchanged true/false
        New-DevSandbox          throwaway RBS_* target directories

    ACCESS RULE: this file opens files under data/ for READING ONLY, via
    Get-FileHash and Get-ChildItem. It never writes, moves or deletes
    anything under data/ under any parameter combination.

.NOTES
    Windows PowerShell 5.1 compatible.
#>

function New-DevDataSnapshot {
    <#
    .SYNOPSIS
        Build a SHA-256 content manifest of a protected data directory.

    .DESCRIPTION
        Read-only. Records, per file, the repo-relative path, SHA-256 hash,
        byte length and last-write timestamp (UTC), plus directory totals.
        A missing directory is a valid, recorded outcome -- not an error.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$DirectoryPath,
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [string]$Label = 'snapshot'
    )

    $files = New-Object System.Collections.ArrayList
    $exists = Test-Path -LiteralPath $DirectoryPath
    $totalBytes = 0

    if ($exists) {
        $items = @(Get-ChildItem -LiteralPath $DirectoryPath -File -Recurse |
                   Sort-Object -Property FullName)
        foreach ($item in $items) {
            $hash = Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256
            $totalBytes = $totalBytes + $item.Length
            [void]$files.Add([pscustomobject]@{
                RelativePath  = (ConvertTo-DevRelativePath -Path $item.FullName -RepoRoot $RepoRoot)
                Sha256        = $hash.Hash
                Bytes         = $item.Length
                LastWriteUtc  = $item.LastWriteTimeUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')
            })
        }
    }

    return [pscustomobject]@{
        Label         = $Label
        DirectoryPath = (ConvertTo-DevRelativePath -Path $DirectoryPath -RepoRoot $RepoRoot)
        Exists        = $exists
        FileCount     = $files.Count
        TotalBytes    = $totalBytes
        GeneratedUtc  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        Files         = @($files)
    }
}

function Compare-DevDataSnapshot {
    <#
    .SYNOPSIS
        Diff two manifests produced by New-DevDataSnapshot.

    .OUTPUTS
        PSCustomObject with Unchanged (bool) plus Added/Removed/Modified.
        Any difference at all is a violation: nothing in the dry-run layer
        is permitted to alter a protected data file.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Before,
        [Parameter(Mandatory = $true)]$After
    )

    $beforeMap = @{}
    foreach ($file in @($Before.Files)) { $beforeMap[$file.RelativePath] = $file }
    $afterMap = @{}
    foreach ($file in @($After.Files)) { $afterMap[$file.RelativePath] = $file }

    $added = New-Object System.Collections.ArrayList
    $removed = New-Object System.Collections.ArrayList
    $modified = New-Object System.Collections.ArrayList

    foreach ($key in $afterMap.Keys) {
        if (-not $beforeMap.ContainsKey($key)) { [void]$added.Add($key) }
    }
    foreach ($key in $beforeMap.Keys) {
        if (-not $afterMap.ContainsKey($key)) {
            [void]$removed.Add($key)
        }
        elseif ($beforeMap[$key].Sha256 -ne $afterMap[$key].Sha256) {
            [void]$modified.Add($key)
        }
    }

    $unchanged = (($added.Count -eq 0) -and ($removed.Count -eq 0) -and ($modified.Count -eq 0))

    return [pscustomobject]@{
        Unchanged = $unchanged
        Added     = @($added)
        Removed   = @($removed)
        Modified  = @($modified)
    }
}

function New-DevSandbox {
    <#
    .SYNOPSIS
        Create the throwaway directories the test run points RBS_* at.

    .DESCRIPTION
        Writes ONLY under .dev/. core.config reads RBS_SQLITE_PATH and
        RBS_STRATEGY_SETS_DIR at import time, so redirecting both means the
        suite physically cannot reach data/oscill8.db or the real Strategy
        Set JSON files -- belt and braces alongside tests/conftest.py's own
        tmp_path database engine.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$SandboxRoot)

    $strategySetsDir = Join-Path $SandboxRoot 'strategy_sets'
    $dbDir = Join-Path $SandboxRoot 'db'

    foreach ($dir in @($SandboxRoot, $strategySetsDir, $dbDir)) {
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
    }

    return [pscustomobject]@{
        Root             = $SandboxRoot
        SqlitePath       = (Join-Path $dbDir 'sandbox.db')
        StrategySetsDir  = $strategySetsDir
    }
}
