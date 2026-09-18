@echo off
setlocal EnableExtensions

rem ---------------------------------------------------------------------
rem Oscill8 launcher.
rem
rem REPOSITORY ROOT IS %~dp0 -- the folder this .bat lives in, never a
rem hard-coded path. The repository can be moved, renamed or cloned
rem elsewhere and this launcher keeps working unchanged.
rem
rem IT STOPS THIS REPOSITORY'S OWN STREAMLIT FIRST. Launching repeatedly
rem otherwise STACKS servers: the new one takes the next free port while
rem the old one keeps serving the original port with whatever code it
rem imported at ITS start time. After an edit that reads as "the app is
rem ignoring my change" -- the browser is simply still pointed at the
rem older server. (Observed live: two instances started seven minutes
rem apart across one edit, rendering different Strategy Labels.)
rem
rem WHAT COUNTS AS "THIS REPOSITORY'S OWN": a python.exe/pythonw.exe
rem whose command line mentions streamlit AND whose executable lives
rem under this repository's own .venv, plus that process's direct
rem children -- on Windows the venv interpreter re-executes the base
rem interpreter, so the process actually serving the port is a child
rem whose own executable path is OUTSIDE the repository. Any other
rem Streamlit process is reported and deliberately LEFT RUNNING. There
rem is no blanket "taskkill /im python.exe" here, and there must not be.
rem ---------------------------------------------------------------------

set "OSCILL8_ROOT=%~dp0"
if "%OSCILL8_ROOT:~-1%"=="\" set "OSCILL8_ROOT=%OSCILL8_ROOT:~0,-1%"

cd /d "%OSCILL8_ROOT%"
if errorlevel 1 (
    echo [oscill8] ERROR: could not enter "%OSCILL8_ROOT%".
    pause
    exit /b 1
)

set "OSCILL8_PY=%OSCILL8_ROOT%\.venv\Scripts\python.exe"
if not exist "%OSCILL8_PY%" (
    echo [oscill8] ERROR: virtual environment interpreter not found:
    echo             "%OSCILL8_PY%"
    echo           Create the .venv first, then re-run this launcher.
    pause
    exit /b 1
)

echo [oscill8] Repository: %OSCILL8_ROOT%
echo [oscill8] Stopping any Streamlit already running for this repository...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=($env:OSCILL8_ROOT).TrimEnd('\')+'\'; $py=@(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe' }); $st=@($py | Where-Object { $_.CommandLine -and $_.CommandLine -match 'streamlit' }); $mine=@($st | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root,[System.StringComparison]::OrdinalIgnoreCase) }); $ids=@($mine | ForEach-Object { $_.ProcessId }); $kids=@($st | Where-Object { $ids -contains $_.ParentProcessId }); $targets=@(@($mine)+@($kids) | Sort-Object ProcessId -Unique); if ($targets.Count -eq 0) { Write-Host '[oscill8]   none running.' }; foreach ($p in $targets) { Write-Host ('[oscill8]   stopping PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; $tid=@($targets | ForEach-Object { $_.ProcessId }); foreach ($p in @($st | Where-Object { $tid -notcontains $_.ProcessId })) { Write-Host ('[oscill8]   NOTE: PID ' + $p.ProcessId + ' runs Streamlit but not from this repository - left alone.') }"

rem Give a stopped server a moment to release its TCP port, so the new
rem instance binds 8501 again instead of silently moving on to 8502.
timeout /t 2 /nobreak >nul 2>&1

echo [oscill8] Starting Streamlit...
"%OSCILL8_PY%" -m streamlit run ui\app.py

endlocal
