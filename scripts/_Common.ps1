<#
.SYNOPSIS
    Shared, side-effect-free helpers for the Oscill8 development harness.

.DESCRIPTION
    This file is DOT-SOURCED by dev.ps1 (and may be dot-sourced manually in
    order to run a single stage by hand):

        . .\scripts\_Common.ps1

    Loading this file defines constants and functions ONLY. It never touches
    Git, never writes to disk, and never runs a test suite.

    Windows PowerShell 5.1 compatible: no '&&'/'||', no ternary, no '??',
    no ConvertFrom-Json -AsHashtable.

.NOTES
    Harness scope: read-only observation. Nothing in this file is permitted
    to mutate Git state, the working tree, or data/.
#>

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Process exit codes. Stable contract -- do not renumber.
$DevExitCodes = @{
    Success       = 0
    Usage         = 1
    Preflight     = 10   # repo/toolchain/path/recursion preflight refused the run
    SpecInvalid   = 20
    BranchFailed  = 30
    TestsUnusable = 40   # the suite could not be run or produced no usable result
    ClaudeFailed  = 50   # Claude could not run, errored, was denied, or did nothing
    GateFailed    = 60   # the suite ran and reported unexpected failures
    DataViolation = 70
    PathViolation = 80   # a change landed outside the task's declared allowed_paths
}

# --- Claude execution layer defaults ---------------------------------------
# Overridable per run by dev.ps1's -ClaudeBudgetUsd / -ClaudeTimeoutSeconds,
# and per machine by these environment variables. Declared here rather than
# inline so the harness has one place to look for its tunables.
$DevClaudeDefaultBudgetUsd = 10.0
if ($env:RBS_CLAUDE_BUDGET_USD) {
    $parsedBudget = 0.0
    if ([double]::TryParse($env:RBS_CLAUDE_BUDGET_USD,
                           [System.Globalization.NumberStyles]::Float,
                           [System.Globalization.CultureInfo]::InvariantCulture,
                           [ref]$parsedBudget) -and $parsedBudget -gt 0) {
        $DevClaudeDefaultBudgetUsd = $parsedBudget
    }
}

# 45 minutes. Long enough for a real multi-file implementation task; the probe
# measured trivial single-turn tasks at 5-16 seconds, so this is not tight.
$DevClaudeDefaultTimeoutSeconds = 2700
if ($env:RBS_CLAUDE_TIMEOUT_SECONDS) {
    $parsedTimeout = 0
    if ([int]::TryParse($env:RBS_CLAUDE_TIMEOUT_SECONDS, [ref]$parsedTimeout) -and $parsedTimeout -gt 0) {
        $DevClaudeDefaultTimeoutSeconds = $parsedTimeout
    }
}

# Repository-relative path prefixes holding live user data. These are never
# staged, stashed, cleaned or modified -- read-only hashing/inspection only.
$DevProtectedPathPrefixes = @(
    'data/'
)

# Known scratch/scaffolding paths that are expected to be dirty and must not
# be reported as an unexplained working-tree change.
#   test_qh.py -- ad-hoc QuantHub probe; performs a LIVE HTTP call at import
#                 time, which is exactly why the test gate scopes pytest to
#                 tests/ instead of running a bare 'pytest -q'.
$DevScratchPaths = @(
    'test_qh.py'
)

# Failures already known to be environmental rather than code defects.
# Documented in README.md's Testing section and in CLAUDE.md's roadmap.
$DevKnownBaselineFailures = @(
    'tests/test_cache.py::test_read_bars_output_matches_downloader_canonical_schema'
)

# Git subcommands Invoke-DevGit is permitted to run. READ-ONLY ONLY.
#
# 'ls-remote' is read-only but reaches the network; callers must set
# GIT_TERMINAL_PROMPT=0 around it so a credential prompt cannot hang the run.
#
# Branch CREATION deliberately does NOT appear here and is NOT routed through
# Invoke-DevGit. The single mutating git call in this harness lives in
# scripts/New-TaskBranch.ps1 (Invoke-DevGitCreateBranch), which builds its own
# fixed argument vector and accepts no caller-supplied arguments -- see that
# file. Keeping Invoke-DevGit strictly read-only means no future caller can
# reach a mutating subcommand through the general-purpose wrapper.
$DevAllowedGitSubcommands = @(
    'rev-parse', 'status', 'branch', 'log', 'diff', 'ls-files',
    'check-ignore', 'remote', 'symbolic-ref', 'show-ref', 'describe',
    'config', 'ls-remote'
)

# Tokens that must never appear anywhere in a git argument list, even if the
# subcommand itself were allow-listed. Defence in depth.
$DevDeniedGitTokens = @(
    'add', 'commit', 'push', 'pull', 'fetch', 'clean', 'stash', 'reset',
    'restore', 'checkout', 'switch', 'merge', 'rebase', 'cherry-pick',
    'revert', 'apply', 'am', 'tag', 'mv', 'rm', 'gc', 'prune',
    'update-ref', 'filter-branch', 'worktree', 'submodule'
)

$script:DevLogPath = $null

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

function Initialize-DevLog {
    <#
    .SYNOPSIS
        Point Write-DevLog at a run log file. Creates the file if absent.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $parent = Split-Path -Path $Path -Parent
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType File -Path $Path | Out-Null
    }
    $script:DevLogPath = $Path
}

function Write-DevLog {
    <#
    .SYNOPSIS
        Write one line to the console and (if initialised) to the run log.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$Message,
        [Parameter(Position = 1)]
        [ValidateSet('INFO', 'OK', 'WARN', 'ERROR', 'STEP')][string]$Level = 'INFO'
    )

    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $line = '[{0}] {1,-5} {2}' -f $stamp, $Level, $Message

    $color = 'Gray'
    if ($Level -eq 'OK')    { $color = 'Green' }
    if ($Level -eq 'WARN')  { $color = 'Yellow' }
    if ($Level -eq 'ERROR') { $color = 'Red' }
    if ($Level -eq 'STEP')  { $color = 'Cyan' }
    Write-Host $line -ForegroundColor $color

    if ($script:DevLogPath) {
        Add-Content -LiteralPath $script:DevLogPath -Value $line -Encoding UTF8
    }
}

# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

function Get-DevTimestamp {
    <#
    .SYNOPSIS
        UTC run stamp, safe as a directory name (no spaces, no colons).
    #>
    [CmdletBinding()]
    param()
    return (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss') + 'Z'
}

function Write-DevTextFile {
    <#
    .SYNOPSIS
        Write UTF-8 (no BOM) text. Avoids Set-Content's ANSI default on 5.1.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content
    )

    $parent = Split-Path -Path $Path -Parent
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Write-DevJsonFile {
    <#
    .SYNOPSIS
        Serialise an object to a UTF-8 JSON artifact under .dev/.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowNull()]$InputObject,
        [int]$Depth = 8
    )

    $json = $InputObject | ConvertTo-Json -Depth $Depth
    Write-DevTextFile -Path $Path -Content $json
}

function ConvertTo-DevRelativePath {
    <#
    .SYNOPSIS
        Repo-relative, forward-slashed path for stable reporting/comparison.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd('\', '/')
    if ($full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        $full = $full.Substring($root.Length)
    }
    return $full.TrimStart('\', '/').Replace('\', '/')
}

# ---------------------------------------------------------------------------
# Repository root
# ---------------------------------------------------------------------------

function Get-DevRepoRoot {
    <#
    .SYNOPSIS
        Walk upward from StartPath until a directory containing .git is found.

    .DESCRIPTION
        Deliberately does NOT rely on the current working directory, so the
        harness behaves identically however it was launched. The caller
        cross-checks the result against 'git rev-parse --show-toplevel'.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StartPath)

    $current = [System.IO.Path]::GetFullPath($StartPath)
    while ($current) {
        if (Test-Path -LiteralPath (Join-Path $current '.git')) {
            return $current
        }
        $parent = Split-Path -Path $current -Parent
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
    return $null
}

# ---------------------------------------------------------------------------
# Guarded Git access
# ---------------------------------------------------------------------------

function Invoke-DevGit {
    <#
    .SYNOPSIS
        Run a read-only git command through an allow-list guard.

    .DESCRIPTION
        Every git call in this harness goes through this one function, so the
        prohibition on add/commit/push/clean/stash/reset/restore/checkout is
        enforced in a single place rather than trusted to each caller.

        Throws if the subcommand is not allow-listed, or if any argument is a
        denied token. Non-zero exit codes are RETURNED, not thrown -- several
        read-only commands (notably check-ignore) use exit status as data.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    if ($Arguments.Count -lt 1) {
        throw 'Invoke-DevGit: no git subcommand supplied.'
    }

    $subcommand = $Arguments[0]
    if ($DevAllowedGitSubcommands -notcontains $subcommand) {
        throw ("Invoke-DevGit: git subcommand '{0}' is not allow-listed (read-only harness)." -f $subcommand)
    }
    foreach ($argument in $Arguments) {
        if ($DevDeniedGitTokens -contains $argument) {
            throw ("Invoke-DevGit: denied git token '{0}' present in argument list." -f $argument)
        }
    }

    Push-Location -LiteralPath $RepoRoot
    try {
        # No 2>&1 here: on 5.1 that wraps native stderr in ErrorRecords and
        # corrupts $? even when the command itself succeeded.
        $output = & git @Arguments
        $code = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    return [pscustomobject]@{
        ExitCode = $code
        Output   = @($output)
        Command  = 'git ' + ($Arguments -join ' ')
    }
}

# ---------------------------------------------------------------------------
# Task specification parsing
# ---------------------------------------------------------------------------

function ConvertFrom-DevFrontMatter {
    <#
    .SYNOPSIS
        Parse the restricted YAML subset used by tasks/*.md front matter.

    .DESCRIPTION
        Supported forms: 'key: scalar', 'key: [a, b]', and a bare 'key:'
        followed by indented '- item' lines. Nothing else -- a task spec that
        needs richer YAML should be simplified rather than growing a parser
        here.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string[]]$Lines)

    $result = @{}
    $currentListKey = $null

    foreach ($rawLine in $Lines) {
        $line = $rawLine.TrimEnd()
        if ($line -match '^\s*$')  { continue }
        if ($line -match '^\s*#')  { continue }

        if ($line -match '^\s+-\s+(.*)$') {
            if ($currentListKey) {
                $item = $Matches[1].Trim().Trim('"').Trim("'")
                $result[$currentListKey] = @($result[$currentListKey]) + $item
            }
            continue
        }

        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$') {
            $key = $Matches[1]
            $value = $Matches[2].Trim()

            if ($value -eq '') {
                $currentListKey = $key
                $result[$key] = @()
                continue
            }

            $currentListKey = $null
            if ($value -match '^\[(.*)\]$') {
                $inner = $Matches[1].Trim()
                if ($inner -eq '') {
                    $result[$key] = @()
                }
                else {
                    $result[$key] = @($inner -split ',' | ForEach-Object { $_.Trim().Trim('"').Trim("'") })
                }
            }
            else {
                $result[$key] = $value.Trim('"').Trim("'")
            }
        }
    }

    return $result
}

function Read-DevTaskSpec {
    <#
    .SYNOPSIS
        Locate, parse and validate tasks/active/<TaskId>*.md.

    .OUTPUTS
        PSCustomObject with IsValid, Errors, Warnings, FrontMatter, Sections.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TaskId,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    $specErrors = New-Object System.Collections.ArrayList
    $specWarnings = New-Object System.Collections.ArrayList
    $activeDir = Join-Path $RepoRoot 'tasks\active'

    if (-not (Test-Path -LiteralPath $activeDir)) {
        [void]$specErrors.Add('tasks/active/ does not exist.')
        return [pscustomobject]@{
            TaskId = $TaskId; Path = $null; RelativePath = $null; IsValid = $false
            Errors = @($specErrors); Warnings = @($specWarnings)
            FrontMatter = @{}; Sections = @()
        }
    }

    $candidates = @(Get-ChildItem -LiteralPath $activeDir -Filter "$TaskId*.md" -File |
                    Sort-Object -Property Name)
    if ($candidates.Count -eq 0) {
        [void]$specErrors.Add("No task specification matching '$TaskId*.md' found in tasks/active/.")
    }
    elseif ($candidates.Count -gt 1) {
        [void]$specErrors.Add("Ambiguous task id '$TaskId': " + (($candidates | ForEach-Object { $_.Name }) -join ', '))
    }

    if ($specErrors.Count -gt 0) {
        return [pscustomobject]@{
            TaskId = $TaskId; Path = $null; RelativePath = $null; IsValid = $false
            Errors = @($specErrors); Warnings = @($specWarnings)
            FrontMatter = @{}; Sections = @()
        }
    }

    $specFile = $candidates[0]
    $lines = @(Get-Content -LiteralPath $specFile.FullName -Encoding UTF8)

    # Front matter must be the very first non-empty construct in the file.
    $fmStart = -1
    $fmEnd = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq '---') {
            if ($fmStart -lt 0) { $fmStart = $i; continue }
            $fmEnd = $i
            break
        }
        if ($fmStart -lt 0 -and $lines[$i].Trim() -ne '') { break }
    }

    $frontMatter = @{}
    if ($fmStart -lt 0 -or $fmEnd -lt 0) {
        [void]$specErrors.Add('Task specification has no "---" delimited front-matter block.')
    }
    else {
        $frontMatter = ConvertFrom-DevFrontMatter -Lines $lines[($fmStart + 1)..($fmEnd - 1)]
    }

    $sections = @($lines |
        Where-Object { $_ -match '^##\s+(.*)$' } |
        ForEach-Object { ($_ -replace '^##\s+', '').Trim() })

    # --- validation --------------------------------------------------------
    $requiredKeys = @('id', 'title', 'branch', 'test_command', 'allowed_paths')
    foreach ($key in $requiredKeys) {
        if (-not $frontMatter.ContainsKey($key)) {
            [void]$specErrors.Add("Front matter is missing required key '$key'.")
        }
    }

    if ($frontMatter.ContainsKey('id') -and $frontMatter['id'] -ne $TaskId) {
        [void]$specErrors.Add(("Front-matter id '{0}' does not match requested task id '{1}'." -f $frontMatter['id'], $TaskId))
    }

    if ($frontMatter.ContainsKey('branch')) {
        if ($frontMatter['branch'] -notmatch '^task/TASK-\d{3}-[a-z0-9-]+$') {
            [void]$specErrors.Add(("Branch name '{0}' does not match the required pattern task/TASK-NNN-<kebab-slug>." -f $frontMatter['branch']))
        }
    }

    if ($frontMatter.ContainsKey('test_command')) {
        if ($frontMatter['test_command'] -notmatch 'tests/') {
            [void]$specErrors.Add("test_command must scope pytest to 'tests/' -- a bare 'pytest -q' also collects root-level scripts that make live network calls.")
        }
    }

    if ($frontMatter.ContainsKey('allowed_paths')) {
        if (@($frontMatter['allowed_paths']).Count -eq 0) {
            [void]$specErrors.Add('allowed_paths is empty; a task must declare the paths it may touch.')
        }
    }

    $expectedSections = @('Objective', 'In scope', 'Out of scope', 'Acceptance criteria')
    foreach ($section in $expectedSections) {
        if ($sections -notcontains $section) {
            [void]$specWarnings.Add("Task specification has no '## $section' section.")
        }
    }

    return [pscustomobject]@{
        TaskId       = $TaskId
        Path         = $specFile.FullName
        RelativePath = (ConvertTo-DevRelativePath -Path $specFile.FullName -RepoRoot $RepoRoot)
        IsValid      = ($specErrors.Count -eq 0)
        Errors       = @($specErrors)
        Warnings     = @($specWarnings)
        FrontMatter  = $frontMatter
        Sections     = $sections
    }
}
