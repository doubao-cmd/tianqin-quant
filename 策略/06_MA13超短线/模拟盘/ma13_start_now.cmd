@echo off
REM ================================================================
REM MA13 simulator - IMMEDIATE start (no 08:55 wait).
REM ASCII only: cmd.exe mangles non-ASCII lines after chcp 65001.
REM Used by the "MA13_sim_now" scheduled task so the simulator can be
REM brought up remotely, without anyone touching the machine.
REM
REM 2026-10-20 (A2): now launches the watchdog in DETACHED mode.
REM   Why: the old version ran the watchdog in THIS console window, so
REM   closing the window killed watchdog + core together -> positions
REM   sat with no stop-loss for 19 minutes on 2026-10-20 20:16-20:35.
REM   With --detach the watchdog lives in its own process with NO
REM   console, and the core gets its own window. Closing ANY window
REM   now only kills the core, and the watchdog brings it back.
REM   => This window will close by itself right after launching. Normal.
REM
REM To really STOP everything (closing a window is NOT enough anymore):
REM   python ma13_watchdog_main.py --stop
REM ================================================================
setlocal
cd /d "%~dp0"

REM ---- python that has tqsdk: MUST be the venv one ----
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] tqsdk venv python not found: %PY%
    pause
    exit /b 1
)
"%PY%" -c "import importlib.util as u,sys; sys.exit(0 if u.find_spec('tqsdk') else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] '%PY%' cannot import tqsdk - refusing to start.
    pause
    exit /b 1
)

REM * 2026-10-18: symbols auto = workbench.json selected (card-C pick).
REM   Change symbols via: run_workbench.py --auto-select --write  (no 5-place edit)
REM * 2026-10-20: --detach = watchdog survives closing this window (A2 fix).
echo [START] MA13 simulator launching (detached watchdog, TqKq cloud sim)...
"%PY%" -u "%~dp0ma13_watchdog_main.py" --symbols auto --trade --detach

echo.
echo [LAUNCHED] the watchdog is now running on its own (no console).
echo           This window can be closed. It already is.
echo           Stop everything with:  python ma13_watchdog_main.py --stop
timeout /t 6 >nul
exit /b 0
