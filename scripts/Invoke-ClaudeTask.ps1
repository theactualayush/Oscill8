<#
.SYNOPSIS
    Headless Claude Code execution layer for the Oscill8 development harness.

.DESCRIPTION
    Dot-source alongside _Common.ps1 and Test-RepoState.ps1:

        . .\scripts\_Common.ps1
        . .\scripts\Test-RepoState.ps1
        . .\scripts\Invoke-ClaudeTask.ps1

    Launches Claude Code as a FILE-EDITING IMPLEMENTATION AGENT ONLY, using
    the tool set empirically validated by the disposable probe:

        --tools Read,Edit,Write,Glob,Grep

    Claude receives NO command execution (Bash/PowerShell), NO web access and
    NO subagent spawning. This is structural, not advisory: a tool that is not
    in --tools is never offered to the model at all, so there is no mechanism
    by which Claude can run git, stage, commit, push, reset, clean or stash.
    The probe confirmed Claude simply reports the tool is unavailable and
    exits cleanly rather than attempting or hanging.

    The harness -- not Claude -- runs the test suite, after Claude exits.

    EMPIRICAL FINDINGS THIS FILE ENCODES (from the probe, see the run report):

      * Claude exits 0 even when it accomplished nothing. Exit code is NOT a
        success signal. Read-DevClaudeResult + a real working-tree diff are.
      * A Windows 8.3 short-name path component (e.g. AYUSH~1.AGA) is treated
        by Claude Code as a suspicious path and silently denies Read/Glob,
        producing exit 0, is_error false, and zero work done. Test-DevPathIsLongForm
        rejects such a path before launch.
      * permission_denials in the JSON result is the machine-readable record
        of blocked tool calls; it was empty on every clean probe run.
      * CLAUDE.md is auto-loaded in --print mode; the prompt references it
        rather than inlining 114 KB of it.

.NOTES
    Windows PowerShell 5.1 compatible. No mutating git command appears here.
#>

# Claude's tool grant. Read/inspect/edit files -- nothing else.
$DevClaudeAllowedTools = 'Read,Edit,Write,Glob,Grep'

# Redundant given the --tools allow-list above (the probe showed --tools alone
# is what removes the capability), kept as belt-and-braces defence in depth.
$DevClaudeDisallowedTools = 'Bash,PowerShell,WebFetch,WebSearch,Agent'

# Environment variables that indicate this process is already inside a Claude
# Code session. Launching a nested Claude from here is refused outright.
$DevClaudeSessionEnvVars = @(
    'CLAUDECODE',
    'CLAUDE_CODE_ENTRYPOINT',
    'CLAUDE_CODE_SESSION_ID',
    'CLAUDE_CODE_BRIDGE_SESSION_ID',
    'CLAUDE_CODE_CHILD_SESSION',
    'CLAUDE_CODE_MESSAGING_SOCKET',
    'CLAUDE_CODE_MESSAGING_TOKEN',
    'CLAUDE_CODE_SSE_PORT',
    'CLAUDE_PID'
)

# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

function Resolve-DevLongPath {
    <#
    .SYNOPSIS
        Best-effort expansion of a Windows 8.3 short path to its long form.

    .DESCRIPTION
        [System.IO.Path]::GetFullPath does NOT expand 8.3 components, so the
        Scripting.FileSystemObject COM object is used, which does. Failure is
        not fatal -- the caller still runs Test-DevPathIsLongForm, which is
        the actual gate.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    try {
        $fso = New-Object -ComObject Scripting.FileSystemObject
        if ($fso.FolderExists($full)) { return $fso.GetFolder($full).Path }
        if ($fso.FileExists($full))   { return $fso.GetFile($full).Path }
    }
    catch {
        # COM unavailable -- fall through to the un-expanded full path.
    }
    return $full
}

function Test-DevPathIsLongForm {
    <#
    .SYNOPSIS
        Reject a path containing an 8.3 short-name component ("~N").

    .DESCRIPTION
        Directly encodes probe finding #11/#12: Claude Code flags a path such
        as C:\Users\AYUSH~1.AGA\... as suspicious and denies file access,
        while still exiting 0 with is_error false. That failure is invisible
        unless the JSON is parsed, so the harness refuses such a path up front
        instead of discovering it afterwards.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $offending = New-Object System.Collections.ArrayList
    foreach ($segment in ($Path -split '[\\/]')) {
        if ($segment -match '~[0-9]') { [void]$offending.Add($segment) }
    }

    return [pscustomobject]@{
        Path               = $Path
        IsLongForm         = ($offending.Count -eq 0)
        OffendingSegments  = @($offending)
    }
}

# ---------------------------------------------------------------------------
# Recursion guard
# ---------------------------------------------------------------------------

function Test-DevClaudeRecursion {
    <#
    .SYNOPSIS
        Detect that this process is already running inside Claude Code.

    .DESCRIPTION
        Launching Claude from inside a Claude session is refused rather than
        worked around: a nested agent would inherit session plumbing
        (messaging socket, session id) and there is no safe, tested behaviour
        for that. The harness is meant to be run by a human from a terminal.
    #>
    [CmdletBinding()]
    param()

    $present = New-Object System.Collections.ArrayList
    foreach ($name in $DevClaudeSessionEnvVars) {
        if (Test-Path -LiteralPath ('Env:' + $name)) { [void]$present.Add($name) }
    }

    return [pscustomobject]@{
        IsRecursive  = ($present.Count -gt 0)
        Detected     = @($present)
    }
}

# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------

function Resolve-DevClaudeExe {
    <#
    .SYNOPSIS
        Locate the canonical Claude Code executable and read its version.

    .DESCRIPTION
        Resolution order:
            1. $env:RBS_CLAUDE_EXE  (explicit override)
            2. <user profile>\.local\bin\claude.exe  (native install)
            3. claude / claude.exe on PATH
            4. failure

        Deliberately never probes the desktop-app-managed copy under
        AppData\Local\Packages\...\claude-code\<version>\claude.exe: that path
        embeds the version number and therefore moves on every auto-update.

        Claude Code auto-updates, so the version is RECORDED per run rather
        than pinned.
    #>
    [CmdletBinding()]
    param()

    $candidates = New-Object System.Collections.ArrayList
    if ($env:RBS_CLAUDE_EXE) {
        [void]$candidates.Add([pscustomobject]@{ Source = 'RBS_CLAUDE_EXE'; Path = $env:RBS_CLAUDE_EXE })
    }

    $profileDir = $env:USERPROFILE
    if (-not $profileDir) { $profileDir = $HOME }
    if ($profileDir) {
        [void]$candidates.Add([pscustomobject]@{
            Source = 'native install'
            Path   = (Join-Path $profileDir '.local\bin\claude.exe')
        })
    }

    $onPath = Get-Command 'claude.exe' -ErrorAction SilentlyContinue
    if (-not $onPath) { $onPath = Get-Command 'claude' -ErrorAction SilentlyContinue }
    if ($onPath -and $onPath.Source) {
        [void]$candidates.Add([pscustomobject]@{ Source = 'PATH'; Path = $onPath.Source })
    }

    $tried = New-Object System.Collections.ArrayList
    foreach ($candidate in $candidates) {
        [void]$tried.Add(('{0}: {1}' -f $candidate.Source, $candidate.Path))
        if (-not (Test-Path -LiteralPath $candidate.Path)) { continue }

        $version = $null
        try {
            $versionOutput = & $candidate.Path '--version'
            if ($LASTEXITCODE -eq 0 -and $versionOutput) {
                $version = (($versionOutput | Select-Object -First 1) -as [string]).Trim()
            }
        }
        catch {
            $version = $null
        }

        if (-not $version) { continue }

        return [pscustomobject]@{
            Resolved = $true
            Path     = (Resolve-DevLongPath -Path $candidate.Path)
            Source   = $candidate.Source
            Version  = $version
            Tried    = @($tried)
            Error    = $null
        }
    }

    return [pscustomobject]@{
        Resolved = $false
        Path     = $null
        Source   = $null
        Version  = $null
        Tried    = @($tried)
        Error    = 'Claude Code executable could not be resolved. Set $env:RBS_CLAUDE_EXE, install the native build to ~\.local\bin\claude.exe, or put claude.exe on PATH.'
    }
}

# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

function New-DevClaudePrompt {
    <#
    .SYNOPSIS
        Build the implementation prompt fed to Claude over stdin.

    .DESCRIPTION
        The task specification is INLINED rather than merely referenced, so
        the instructions cannot depend on Claude choosing to read the file.
        CLAUDE.md is only referenced: the probe confirmed it is auto-loaded in
        --print mode, and Oscill8's is ~115 KB.

        The prompt is delivered via stdin redirection, never as a positional
        argument -- the repository path contains spaces and PowerShell 5.1
        argument quoting is unreliable.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Spec,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    $specText = Get-Content -LiteralPath $Spec.Path -Raw -Encoding UTF8

    $allowed = @($Spec.FrontMatter['allowed_paths'])
    $allowedList = ($allowed | ForEach-Object { '  - ' + $_ }) -join "`r`n"

    $forbidden = @()
    if ($Spec.FrontMatter.ContainsKey('forbidden_paths')) {
        $forbidden = @($Spec.FrontMatter['forbidden_paths'])
    }
    $forbiddenList = ''
    if ($forbidden.Count -gt 0) {
        $forbiddenList = ($forbidden | ForEach-Object { '  - ' + $_ }) -join "`r`n"
    }

    $docs = @()
    if ($Spec.FrontMatter.ContainsKey('allow_doc_updates')) {
        $docs = @($Spec.FrontMatter['allow_doc_updates'])
    }
    $docsList = '  (none)'
    if ($docs.Count -gt 0) {
        $docsList = ($docs | ForEach-Object { '  - ' + $_ }) -join "`r`n"
    }

    $builder = New-Object System.Text.StringBuilder
    [void]$builder.AppendLine('You are implementing one task in the Oscill8 repository.')
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine('CONTEXT')
    [void]$builder.AppendLine('  The repository CLAUDE.md is already loaded into your context and is the')
    [void]$builder.AppendLine('  authoritative source of truth for this project. Follow it. Read the')
    [void]$builder.AppendLine('  module sections the task names before you change anything.')
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine(('  Repository root : ' + $RepoRoot))
    [void]$builder.AppendLine(('  Task file       : ' + $Spec.RelativePath))
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine('YOUR TOOLS')
    [void]$builder.AppendLine('  You have Read, Edit, Write, Glob and Grep only.')
    [void]$builder.AppendLine('  You deliberately have NO shell, NO Bash, NO PowerShell, NO git execution,')
    [void]$builder.AppendLine('  NO web access and NO subagents. This is intentional, not a misconfiguration.')
    [void]$builder.AppendLine('  Do not attempt to run commands and do not ask for them to be enabled.')
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine('WHAT TO DO')
    [void]$builder.AppendLine('  1. Read the task specification reproduced below.')
    [void]$builder.AppendLine('  2. Inspect the repository files relevant to it.')
    [void]$builder.AppendLine('  3. Implement the task, including any tests the task calls for.')
    [void]$builder.AppendLine('  4. Stop when the implementation is complete.')
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine('HARD RULES')
    [void]$builder.AppendLine('  * Modify ONLY files under these allowed paths:')
    [void]$builder.AppendLine($allowedList)
    [void]$builder.AppendLine('  * Documentation files you may additionally update:')
    [void]$builder.AppendLine($docsList)
    if ($forbiddenList) {
        [void]$builder.AppendLine('  * Never modify anything under these forbidden paths:')
        [void]$builder.AppendLine($forbiddenList)
    }
    [void]$builder.AppendLine('  * NEVER touch data/ -- it holds live user data (Strategy Set JSON files and')
    [void]$builder.AppendLine('    the SQLite cache) that is untracked and has no version history.')
    [void]$builder.AppendLine('  * Do not modify unrelated files, and do not tidy or refactor code the task')
    [void]$builder.AppendLine('    did not ask you to change.')
    [void]$builder.AppendLine('  * Do not commit, stage or push anything. You have no tool that can, and the')
    [void]$builder.AppendLine('    harness handles version control itself.')
    [void]$builder.AppendLine('  * Do NOT try to run the test suite. The harness runs "pytest -q tests/"')
    [void]$builder.AppendLine('    after you exit and reports the result. Write the code carefully instead.')
    [void]$builder.AppendLine('  * If the task turns out to be blocked or to require changes outside the')
    [void]$builder.AppendLine('    allowed paths, stop and explain rather than widening the scope.')
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine('WHEN YOU FINISH')
    [void]$builder.AppendLine('  Summarise, in your final message: which files you changed, what you')
    [void]$builder.AppendLine('  implemented, anything you deliberately did not do, and anything you are')
    [void]$builder.AppendLine('  uncertain about. This summary is captured in the run report for human review.')
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine('==================== TASK SPECIFICATION ====================')
    [void]$builder.AppendLine('')
    [void]$builder.AppendLine($specText)

    return $builder.ToString()
}

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

function Invoke-DevClaudeTask {
    <#
    .SYNOPSIS
        Run Claude Code headlessly against the repository, with a hard timeout.

    .DESCRIPTION
        --debug-file is passed as a REPOSITORY-RELATIVE forward-slashed path
        with the working directory set to the repository root, for the same
        reason Invoke-DevTestSuite passes --junitxml that way: the absolute
        path contains spaces (OneDrive) and PowerShell 5.1's Start-Process
        does not quote ArgumentList elements for you. --add-dir needs an
        absolute path, so it is quoted explicitly.

        This session's own CLAUDE_CODE_* variables are stripped for the child
        process. The caller should already have refused to get here via
        Test-DevClaudeRecursion; this is a second line of defence so a stale
        variable cannot leak session plumbing into the child.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$ClaudeExe,
        [Parameter(Mandatory = $true)][string]$PromptPath,
        [Parameter(Mandatory = $true)][string]$StdOutPath,
        [Parameter(Mandatory = $true)][string]$StdErrPath,
        [Parameter(Mandatory = $true)][string]$DebugRelativePath,
        [Parameter(Mandatory = $true)][string]$SessionId,
        [Parameter(Mandatory = $true)][double]$MaxBudgetUsd,
        [int]$TimeoutSeconds = 1800
    )

    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    $claudeArgs = @(
        '--print',
        '--output-format', 'json',
        '--permission-mode', 'acceptEdits',
        '--tools', $DevClaudeAllowedTools,
        '--disallowedTools', $DevClaudeDisallowedTools,
        '--add-dir', ('"' + $RepoRoot + '"'),
        '--session-id', $SessionId,
        '--max-budget-usd', $MaxBudgetUsd.ToString($invariant),
        '--debug-file', $DebugRelativePath
    )

    $displayCommand = '"{0}" {1}' -f $ClaudeExe, ($claudeArgs -join ' ')

    $saved = @{}
    foreach ($name in $DevClaudeSessionEnvVars) {
        $envPath = 'Env:' + $name
        if (Test-Path -LiteralPath $envPath) {
            $saved[$name] = (Get-Item -LiteralPath $envPath).Value
            Remove-Item -LiteralPath $envPath
        }
    }

    $startedUtc = (Get-Date).ToUniversalTime()
    $exitCode = $null
    $timedOut = $false
    $launchError = $null

    try {
        $process = Start-Process -FilePath $ClaudeExe `
                                 -ArgumentList $claudeArgs `
                                 -WorkingDirectory $RepoRoot `
                                 -NoNewWindow `
                                 -PassThru `
                                 -RedirectStandardInput $PromptPath `
                                 -RedirectStandardOutput $StdOutPath `
                                 -RedirectStandardError $StdErrPath

        try {
            Wait-Process -InputObject $process -Timeout $TimeoutSeconds -ErrorAction Stop
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
        foreach ($key in $saved.Keys) {
            Set-Item -LiteralPath ('Env:' + $key) -Value $saved[$key]
        }
    }

    $finishedUtc = (Get-Date).ToUniversalTime()

    return [pscustomobject]@{
        CommandLine      = $displayCommand
        WorkingDirectory = $RepoRoot
        SessionId        = $SessionId
        MaxBudgetUsd     = $MaxBudgetUsd
        TimeoutSeconds   = $TimeoutSeconds
        ExitCode         = $exitCode
        TimedOut         = $timedOut
        LaunchError      = $launchError
        StartedUtc       = $startedUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')
        DurationSeconds  = [math]::Round(($finishedUtc - $startedUtc).TotalSeconds, 1)
        StdOutPath       = $StdOutPath
        StdErrPath       = $StdErrPath
        PromptPath       = $PromptPath
    }
}

# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

function Read-DevClaudeResult {
    <#
    .SYNOPSIS
        Parse the single JSON object emitted by --output-format json.

    .DESCRIPTION
        This -- not the process exit code -- is the authoritative record of
        what Claude did. The probe observed exit code 0 on a run that
        accomplished nothing because every file read was silently denied.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{
            IsParsed = $false; ParseError = "Claude JSON output not found at '$Path'."
            IsError = $true; Subtype = $null; ResultText = $null; SessionId = $null
            NumTurns = 0; DurationMs = 0; TotalCostUsd = 0
            PermissionDenials = @(); TerminalReason = $null; ApiErrorStatus = $null
        }
    }

    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $raw -or $raw.Trim() -eq '') {
        return [pscustomobject]@{
            IsParsed = $false; ParseError = 'Claude produced no output at all.'
            IsError = $true; Subtype = $null; ResultText = $null; SessionId = $null
            NumTurns = 0; DurationMs = 0; TotalCostUsd = 0
            PermissionDenials = @(); TerminalReason = $null; ApiErrorStatus = $null
        }
    }

    try {
        $json = $raw | ConvertFrom-Json
    }
    catch {
        return [pscustomobject]@{
            IsParsed = $false; ParseError = ("Claude output was not valid JSON: " + $_.Exception.Message)
            IsError = $true; Subtype = $null; ResultText = $null; SessionId = $null
            NumTurns = 0; DurationMs = 0; TotalCostUsd = 0
            PermissionDenials = @(); TerminalReason = $null; ApiErrorStatus = $null
        }
    }

    function Get-JsonValue { param($Object, [string]$Name, $Default = $null)
        if ($Object -and ($Object.PSObject.Properties.Name -contains $Name)) { return $Object.$Name }
        return $Default
    }

    $denials = @(Get-JsonValue -Object $json -Name 'permission_denials' -Default @())

    return [pscustomobject]@{
        IsParsed          = $true
        ParseError        = $null
        IsError           = [bool](Get-JsonValue -Object $json -Name 'is_error' -Default $false)
        Subtype           = (Get-JsonValue -Object $json -Name 'subtype')
        ResultText        = (Get-JsonValue -Object $json -Name 'result')
        SessionId         = (Get-JsonValue -Object $json -Name 'session_id')
        NumTurns          = (Get-JsonValue -Object $json -Name 'num_turns' -Default 0)
        DurationMs        = (Get-JsonValue -Object $json -Name 'duration_ms' -Default 0)
        TotalCostUsd      = (Get-JsonValue -Object $json -Name 'total_cost_usd' -Default 0)
        PermissionDenials = $denials
        TerminalReason    = (Get-JsonValue -Object $json -Name 'terminal_reason')
        ApiErrorStatus    = (Get-JsonValue -Object $json -Name 'api_error_status')
    }
}

# ---------------------------------------------------------------------------
# Post-run working-tree inspection (read-only)
# ---------------------------------------------------------------------------

function Get-DevWorkingTreeChanges {
    <#
    .SYNOPSIS
        Determine, read-only, what changed in the working tree.

    .DESCRIPTION
        Reuses Get-DevDirtyClassification's existing taxonomy rather than
        inventing a second one. Protected (data/) and declared-scratch paths
        are reported separately and never counted as task changes.

        The caller is expected to have required a clean-except-Protected/Scratch
        tree before launching Claude, so anything classified DirtyTracked or
        UnknownUntracked afterwards is attributable to Claude.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RepoRoot)

    $statusResult = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @('status', '--porcelain')
    $statusLines = @($statusResult.Output | Where-Object { $_ -ne $null -and $_ -ne '' })
    $classified = Get-DevDirtyClassification -StatusLines $statusLines

    $diffNames = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @('diff', '--name-only')
    $stagedNames = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @('diff', '--cached', '--name-only')
    $diffStat = Invoke-DevGit -RepoRoot $RepoRoot -Arguments @('diff', '--stat')

    $modifiedTracked = @($diffNames.Output | Where-Object { $_ -ne $null -and $_ -ne '' } |
                         ForEach-Object { ([string]$_).Trim().Replace('\', '/') })
    $stagedTracked = @($stagedNames.Output | Where-Object { $_ -ne $null -and $_ -ne '' } |
                       ForEach-Object { ([string]$_).Trim().Replace('\', '/') })

    $newUntracked = @($classified |
        Where-Object { $_.Classification -eq 'UnknownUntracked' } |
        ForEach-Object { $_.Path })
    $protectedTouched = @($classified |
        Where-Object { $_.Classification -eq 'Protected' } |
        ForEach-Object { $_.Path })

    $changedPaths = @($modifiedTracked + $newUntracked) | Sort-Object -Unique

    return [pscustomobject]@{
        StatusLines      = @($statusLines)
        Classified       = @($classified)
        ModifiedTracked  = @($modifiedTracked)
        StagedTracked    = @($stagedTracked)
        NewUntracked     = @($newUntracked)
        ProtectedTouched = @($protectedTouched)
        ChangedPaths     = @($changedPaths)
        HasChanges       = (@($changedPaths).Count -gt 0)
        DiffStat         = @($diffStat.Output)
    }
}

function Test-DevChangedPathsInScope {
    <#
    .SYNOPSIS
        Check every changed path against the task's declared allowed_paths.

    .DESCRIPTION
        VERIFICATION, NOT ENFORCEMENT. The harness cannot prevent Claude from
        editing an out-of-scope file; it can only detect it afterwards and
        fail the run. Permitted = allowed_paths (directory prefixes or exact
        files) plus allow_doc_updates (exact files).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$ChangedPaths,
        [Parameter(Mandatory = $true)]$Spec
    )

    $allowed = @($Spec.FrontMatter['allowed_paths'])
    $docs = @()
    if ($Spec.FrontMatter.ContainsKey('allow_doc_updates')) {
        $docs = @($Spec.FrontMatter['allow_doc_updates'])
    }

    $violations = New-Object System.Collections.ArrayList
    $inScope = New-Object System.Collections.ArrayList

    foreach ($path in $ChangedPaths) {
        $normalised = $path.Replace('\', '/').TrimStart('./')
        $permitted = $false

        foreach ($prefix in $allowed) {
            $p = ([string]$prefix).Replace('\', '/')
            if ($p.EndsWith('/')) {
                if ($normalised.StartsWith($p, [System.StringComparison]::OrdinalIgnoreCase)) { $permitted = $true; break }
            }
            elseif ($normalised -eq $p -or $normalised.StartsWith($p + '/', [System.StringComparison]::OrdinalIgnoreCase)) {
                $permitted = $true; break
            }
        }

        if (-not $permitted) {
            foreach ($doc in $docs) {
                if ($normalised -eq ([string]$doc).Replace('\', '/')) { $permitted = $true; break }
            }
        }

        if ($permitted) { [void]$inScope.Add($path) }
        else { [void]$violations.Add($path) }
    }

    return [pscustomobject]@{
        AllInScope = ($violations.Count -eq 0)
        InScope    = @($inScope)
        Violations = @($violations)
        Permitted  = @(@($allowed) + @($docs))
    }
}
