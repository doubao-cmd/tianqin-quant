@echo off
REM ================================================================
REM MA13 simulator - IMMEDIATE start (no 08:55 wait).
REM ASCII only: cmd.exe mangles non-ASCII lines after chcp 65001.
REM Used by the "MA13_sim_now" scheduled task so the simulator can be
REM brought up remotely, without anyone touching the machine.
REM ================================================================
setlocal
cd /d "%~dp0"

REM ---- python that has tqsdk: MUST be the venv one ----
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] tqsdk venv python not found: %PY%
    exit /b 1
)
"%PY%" -c "import importlib.util as u,sys; sys.exit(0 if u.find_spec('tqsdk') else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] '%PY%' cannot import tqsdk - refusing to start.
    exit /b 1
)

REM * 2026-10-18: symbols auto = workbench.json selected (card-C pick).
REM   Change symbols via: run_workbench.py --auto-select --write  (no 5-place edit)
echo [START] MA13 simulator launching (workbench selected, TqKq cloud sim)...
"%PY%" -u "%~dp0ma13_watchdog_main.py" --symbols auto --trade

echo.
echo [EXIT] simulator stopped.
exit /b 0