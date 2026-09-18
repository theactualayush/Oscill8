<#
.SYNOPSIS
    Run the Oscill8 pytest suite under a sandboxed environment and parse the
    resulting JUnit XML.

.DESCRIPTION
    Dot-source alongside _Common.ps1:

        . .\scripts\_Common.ps1
        . .\scripts\Invoke-Oscill8Tests.ps1

    Two hard rules encoded here:

    1. NEVER a bare 'pytest -q'. Collection is scoped to tests/ because the
       repository root also holds test_qh.py, which performs a live QuantHub
       HTTP request at import time, and test_intermarket.py, a standalone
       LSEG/QuantHub validation harness. A rootdir-wide collection would
       execute real network calls just to enumerate tests.

    2. ALWAYS the repository's own interpreter, .venv\Scripts\python.exe --
       never an ambient 'pytest' from PATH.

    RBS_SQLITE_PATH and RBS_STRATEGY_SETS_DIR are redirected into the .dev
    sandbox for the child process and restored afterwards, so the suite
    cannot reach data/oscill8.db or data/strategy_sets/.

    3. The task's declared 'test_command' is AUTHORITATIVE, but is parsed --
       never executed as a command line. ConvertTo-DevPytestArgs turns the
       declared string into an argument vector for the repository's own
       interpreter, and refuses anything that is not a pytest invocation. The
       harness has no shell, so there is nothing for a metacharacter to reach;
       rejecting them anyway keeps the contract explicit and auditable rather
       than relying on that absence.

.NOTES
    Windows PowerShell 5.1 compatible.
#>

# The only command heads a declared test_command may use. Everything else is
# refused: this file runs pytest, it is not a general command runner.
$DevTestCommandHeads = @('pytest', 'python -m pytest', 'py -m pytest')

# Characters that would be meaningful to a shell. No shell is involved here
# (Start-Process receives an argument vector, not a command line), so these can
# never be interpreted -- they are rejected so that a task file declaring one
# fails loudly instead of silently having it treated as a literal pytest
# argument.
$DevTestCommandForbiddenChars = @(';', '&', '|', '>', '<', '`', '$', '(', ')', '{', '}', '^', "`n", "`r")

function ConvertTo-DevPytestArgs {
    <#
    .SYNOPSIS
        Parse a declared test_command into a python.exe argument vector.

    .DESCRIPTION
        Accepts 'pytest ...', 'python -m pytest ...' or 'py -m pytest ...' and
        returns @('-m','pytest', <declared arguments>, '--junitxml=<path>',
        '-p','no:cacheprovider').

        The two injected arguments are not negotiable: the JUnit XML is how the
        harness learns what passed, and the cache provider is disabled so a run
        never writes .pytest_cache into the working tree it is about to verify.
        A declared command that supplies its own --junitxml is refused rather
        than silently overridden.

        Returns an object with IsValid/Error/Arguments so the caller can report
        a bad task file precisely instead of failing somewhere inside pytest.

    .OUTPUTS
        PSCustomObject: IsValid, Error, Arguments, Tokens, TestCommand.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$TestCommand,
        [Parameter(Mandatory = $true)][string]$JUnitRelativePath
    )

    $invalid = {
        param([string]$Message)
        return [pscustomobject]@{
            IsValid     = $false
            Error       = $Message
            Arguments   = @()
            Tokens      = @()
            TestCommand = $TestCommand
        }
    }

    $command = ([string]$TestCommand).Trim()
    if (-not $command) {
        return (& $invalid 'test_command is empty.')
    }

    foreach ($char in $DevTestCommandForbiddenChars) {
        if ($command.Contains($char)) {
            return (& $invalid ("test_command contains the forbidden character '{0}'. It is parsed as a pytest invocation, never executed as a shell command line." -f $char))
        }
    }

    # Whitespace tokenisation honouring simple double-quoted spans, e.g.
    #   pytest -q tests/ -k "not slow"
    $tokens = New-Object System.Collections.ArrayList
    $current = New-Object System.Text.StringBuilder
    $inQuote = $false
    foreach ($char in $command.ToCharArray()) {
        if ($char -eq '"') { $inQuote = -not $inQuote; continue }
        if ((-not $inQuote) -and ($char -eq ' ' -or $char -eq "`t")) {
            if ($current.Length -gt 0) {
                [void]$tokens.Add($current.ToString())
                [void]$current.Clear()
            }
            continue
        }
        [void]$current.Append($char)
    }
    if ($inQuote) {
        return (& $invalid 'test_command has an unterminated double quote.')
    }
    if ($current.Length -gt 0) { [void]$tokens.Add($current.ToString()) }

    $tokenList = @($tokens)
    if ($tokenList.Count -lt 1) {
        return (& $invalid 'test_command contains no tokens.')
    }

    # Strip the accepted head, whichever form it takes.
    $rest = @()
    if ($tokenList[0] -eq 'pytest') {
        $rest = @($tokenList[1..($tokenList.Count - 1)])
        if ($tokenList.Count -eq 1) { $rest = @() }
    }
    elseif (($tokenList[0] -eq 'python' -or $tokenList[0] -eq 'py') -and
            $tokenList.Count -ge 3 -and $tokenList[1] -eq '-m' -and $tokenList[2] -eq 'pytest') {
        $rest = @()
        if ($tokenList.Count -gt 3) { $rest = @($tokenList[3..($tokenList.Count - 1)]) }
    }
    else {
        return (& $invalid ("test_command must start with 'pytest', 'python -m pytest' or 'py -m pytest' -- got '{0}'. The harness runs pytest through the repository interpreter; it cannot run arbitrary commands." -f $tokenList[0]))
    }

    foreach ($token in $rest) {
        if ($token -like '--junitxml*') {
            return (& $invalid 'test_command must not supply --junitxml; the harness supplies its own so it can parse the results.')
        }
    }

    $arguments = @('-m', 'pytest') + @($rest) + @(('--junitxml=' + $JUnitRelativePath), '-p', 'no:cacheprovider')

    return [pscustomobject]@{
        IsValid     = $true
        Error       = $null
        Arguments   = @($arguments)
        Tokens      = @($tokenList)
        TestCommand = $command
    }
}

function Invoke-DevTestSuite {
    <#
    .SYNOPSIS
        Execute pytest and return the raw run outcome (not a pass/fail gate).

    .DESCRIPTION
        The JUnit path is passed to pytest as a REPOSITORY-RELATIVE, forward
        -slashed path with the working directory set to the repository root.
        That is deliberate: the absolute path contains spaces (OneDrive), and
        PowerShell 5.1's Start-Process argument quoting is unreliable with
        embedded spaces. Console redirection targets are ordinary PowerShell
        parameters, so those stay absolute and quote correctly.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$JUnitRelativePath,
        [Parameter(Mandatory = $true)][string]$StdOutPath,
        [Parameter(Mandatory = $true)][string]$StdErrPath,
        [Parameter(Mandatory = $true)][string]$SandboxSqlitePath,
        [Parameter(Mandatory = $true)][string]$SandboxStrategySetsDir,
        [int]$TimeoutSeconds = 1800,
        [string]$TestCommand = 'pytest -q tests/'
    )

    # The declared test_command is authoritative, parsed (never executed) by
    # ConvertTo-DevPytestArgs. The default reproduces the historical argument
    # vector exactly, so a caller that supplies nothing behaves as before.
    $parsed = ConvertTo-DevPytestArgs -TestCommand $TestCommand -JUnitRelativePath $JUnitRelativePath
    if (-not $parsed.IsValid) {
        return [pscustomobject]@{
            CommandLine       = $null
            WorkingDirectory  = $RepoRoot
            JUnitRelativePath = $JUnitRelativePath
            JUnitPath         = (Join-Path $RepoRoot ($JUnitRelativePath -replace '/', '\'))
            StdOutPath        = $StdOutPath
            StdErrPath        = $StdErrPath
            ExitCode          = $null
            TimedOut          = $false
            LaunchError       = $parsed.Error
            StartedUtc        = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            DurationSeconds   = 0
            PytestSummaryLine = $null
            SandboxSqlite     = $SandboxSqlitePath
            SandboxSets       = $SandboxStrategySetsDir
            TestCommand       = $TestCommand
        }
    }

    $pytestArgs = @($parsed.Arguments)

    $commandLine = '"{0}" {1}' -f $PythonExe, ($pytestArgs -join ' ')

    # --- redirect RBS_* for the child, restore afterwards ------------------
    $previousSqlite = $env:RBS_SQLITE_PATH
    $previousSets = $env:RBS_STRATEGY_SETS_DIR
    $hadSqlite = Test-Path -LiteralPath 'Env:RBS_SQLITE_PATH'
    $hadSets = Test-Path -LiteralPath 'Env:RBS_STRATEGY_SETS_DIR'

    $startedUtc = (Get-Date).ToUniversalTime()
    $exitCode = $null
    $timedOut = $false
    $launchError = $null

    try {
        $env:RBS_SQLITE_PATH = $SandboxSqlitePath
        $env:RBS_STRATEGY_SETS_DIR = $SandboxStrategySetsDir

        $process = Start-Process -FilePath $PythonExe `
                                 -ArgumentList $pytestArgs `
                                 -WorkingDirectory $RepoRoot `
                                 -NoNewWindow `
                                 -PassThru `
                                 -RedirectStandardOutput $StdOutPath `
                                 -RedirectStandardError $StdErrPath

        try {
            Wait-Process -InputObject $process -Timeout $TimeoutSeconds -ErrorAction Stop
            $exitCode = $process.ExitCode
        }
        catch {
            $timedOut = $true
            try {
                Stop-Process -InputObject $process -Force -ErrorAction Stop
            }
            catch {
                # Process may have exited between the timeout and the kill.
            }
        }
    }
    catch {
        $launchError = $_.Exception.Message
    }
    finally {
        if ($hadSqlite) { $env:RBS_SQLITE_PATH = $previousSqlite }
        else { Remove-Item -LiteralPath 'Env:RBS_SQLITE_PATH' -ErrorAction SilentlyContinue }
        if ($hadSets) { $env:RBS_STRATEGY_SETS_DIR = $previousSets }
        else { Remove-Item -LiteralPath 'Env:RBS_STRATEGY_SETS_DIR' -ErrorAction SilentlyContinue }
    }

    $finishedUtc = (Get-Date).ToUniversalTime()

    $summaryLine = $null
    if (Test-Path -LiteralPath $StdOutPath) {
        $tail = @(Get-Content -LiteralPath $StdOutPath -Tail 20 |
                  Where-Object { $_ -match 'passed|failed|error|skipped|no tests ran' })
        if ($tail.Count -gt 0) { $summaryLine = $tail[-1].Trim() }
    }

    return [pscustomobject]@{
        CommandLine       = $commandLine
        WorkingDirectory  = $RepoRoot
        JUnitRelativePath = $JUnitRelativePath
        JUnitPath         = (Join-Path $RepoRoot ($JUnitRelativePath -replace '/', '\'))
        StdOutPath        = $StdOutPath
        StdErrPath        = $StdErrPath
        ExitCode          = $exitCode
        TimedOut          = $timedOut
        LaunchError       = $launchError
        StartedUtc        = $startedUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')
        DurationSeconds   = [math]::Round(($finishedUtc - $startedUtc).TotalSeconds, 1)
        PytestSummaryLine = $summaryLine
        SandboxSqlite     = $SandboxSqlitePath
        SandboxSets       = $SandboxStrategySetsDir
        TestCommand       = $TestCommand
    }
}

function ConvertTo-DevNodeId {
    <#
    .SYNOPSIS
        Build a pytest-style node id from a JUnit <testcase> element.

    .DESCRIPTION
        pytest's junitxml emits a 'file' attribute (already repo-relative,
        e.g. tests/test_cache.py) which is used when present. The classname
        fallback converts dotted module paths back to a file path, treating a
        leading-uppercase segment as a test class rather than a directory.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$TestCase)

    $name = [string]$TestCase.name

    $file = $null
    if ($TestCase.PSObject.Properties.Name -contains 'file') {
        $file = [string]$TestCase.file
    }
    if ($file) {
        return ($file -replace '\\', '/') + '::' + $name
    }

    $classname = ''
    if ($TestCase.PSObject.Properties.Name -contains 'classname') {
        $classname = [string]$TestCase.classname
    }
    if (-not $classname) {
        # Collection-level entries (e.g. a module-wide skip) carry an empty
        # classname and put the dotted module path in 'name' instead.
        if ($name -match '^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$') {
            return ($name -replace '\.', '/') + '.py'
        }
        return $name
    }

    $segments = @($classname -split '\.')
    $pathSegments = New-Object System.Collections.ArrayList
    $classSegments = New-Object System.Collections.ArrayList
    $inClass = $false
    foreach ($segment in $segments) {
        if (-not $inClass -and $segment -cmatch '^[A-Z]') { $inClass = $true }
        if ($inClass) { [void]$classSegments.Add($segment) }
        else { [void]$pathSegments.Add($segment) }
    }

    $nodeId = ($pathSegments -join '/') + '.py'
    if ($classSegments.Count -gt 0) {
        $nodeId = $nodeId + '::' + ($classSegments -join '::')
    }
    return $nodeId + '::' + $name
}

function Read-DevJUnitResults {
    <#
    .SYNOPSIS
        Parse a pytest JUnit XML file into counts and failing node ids.

    .OUTPUTS
        PSCustomObject with IsParsed plus totals and node-id lists. Node-id
        lists are what a future run-to-run delta gate will compare; the dry
        -run layer only records them as the baseline.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{
            IsParsed = $false
            ParseError = "JUnit XML not found at '$Path'."
            Total = 0; Passed = 0; Failed = 0; Errors = 0; Skipped = 0
            DurationSeconds = 0
            FailedNodeIds = @(); ErrorNodeIds = @(); SkippedNodeIds = @()
        }
    }

    try {
        [xml]$document = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    }
    catch {
        return [pscustomobject]@{
            IsParsed = $false
            ParseError = "Failed to parse JUnit XML: $($_.Exception.Message)"
            Total = 0; Passed = 0; Failed = 0; Errors = 0; Skipped = 0
            DurationSeconds = 0
            FailedNodeIds = @(); ErrorNodeIds = @(); SkippedNodeIds = @()
        }
    }

    $suites = @($document.SelectNodes('//testsuite'))
    $testCases = @($document.SelectNodes('//testcase'))

    $total = 0
    $failed = 0
    $errored = 0
    $skipped = 0
    $duration = 0.0
    foreach ($suite in $suites) {
        if ($suite.tests)    { $total = $total + [int]$suite.tests }
        if ($suite.failures) { $failed = $failed + [int]$suite.failures }
        if ($suite.errors)   { $errored = $errored + [int]$suite.errors }
        if ($suite.skipped)  { $skipped = $skipped + [int]$suite.skipped }
        if ($suite.time)     { $duration = $duration + [double]$suite.time }
    }

    $failedNodeIds = New-Object System.Collections.ArrayList
    $errorNodeIds = New-Object System.Collections.ArrayList
    $skippedNodeIds = New-Object System.Collections.ArrayList

    foreach ($testCase in $testCases) {
        $nodeId = ConvertTo-DevNodeId -TestCase $testCase
        if ($testCase.SelectSingleNode('failure')) { [void]$failedNodeIds.Add($nodeId) }
        if ($testCase.SelectSingleNode('error'))   { [void]$errorNodeIds.Add($nodeId) }
        if ($testCase.SelectSingleNode('skipped')) { [void]$skippedNodeIds.Add($nodeId) }
    }

    $passed = $total - $failed - $errored - $skipped
    if ($passed -lt 0) { $passed = 0 }

    return [pscustomobject]@{
        IsParsed        = $true
        ParseError      = $null
        Total           = $total
        Passed          = $passed
        Failed          = $failed
        Errors          = $errored
        Skipped         = $skipped
        DurationSeconds = [math]::Round($duration, 1)
        FailedNodeIds   = @($failedNodeIds)
        ErrorNodeIds    = @($errorNodeIds)
        SkippedNodeIds  = @($skippedNodeIds)
    }
}
