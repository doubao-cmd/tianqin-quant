@echo off
REM ============================================================
REM MA13 SIM auto-start wrapper (ASCII only by design)
REM   1) Scheduled Task (recommended): trigger 08:55 Mon-Fri
REM   2) Startup-folder shortcut: this file waits until 08:55
REM   Manual immediate start (skip wait):
REM     start "" cmd /c "set START_NOW=1 && "%~f0""
REM WHY ASCII: any Chinese line after "chcp 65001" gets swallowed
REM     by cmd.exe line by line (verified 2026-10-07).
REM
REM 2026-10-20 (A2): watchdog is now launched DETACHED (--detach).
REM   Closing any window no longer stops the simulator; the watchdog
REM   keeps running and will restart the core. To stop everything:
REM       python ma13_watchdog_main.py --stop
REM ============================================================
setlocal

REM ---- wait until 08:55 (START_NOW=1 = start immediately) ----
REM %TIME% gives " 8:55:00" on single-digit hours, so normalise to
REM a 3-4 digit number: 8:55 -> 855, 08:55 -> 0855 (both = 855).
if not defined START_NOW (
    :WAIT_LOOP
    set "T=%TIME: =%"
    set "HM=%T:~0,5%"
    set "HM=%HM::=%"
    if "%HM%" LSS "855" goto :DO_WAIT
    goto :GO_START
    :DO_WAIT
    echo [WAIT] now %HM% - waiting until 08:55 ...
    ping -n 61 127.0.0.1 >nul
    goto :WAIT_LOOP
)
:GO_START

REM ---- find project root (the dir holding config_symbols.py) ----
set "ROOT=%~dp0"
for %%I in ("%ROOT%..\..\..\..") do set "ROOT=%%~fI\"

REM ---- python that has tqsdk: MUST be the venv one.
REM The fallback list used to include system python/py, which have NO tqsdk
REM and die as silent 1-thread / 0-CPU shells. Fail loudly instead.
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

echo ============================================================
echo  MA13 SIM auto start - REAL ORDERS on TqKq cloud account
echo  symbols: auto = workbench.json selected (card-C two-stage pick)
echo  guards : DD fuse 20pct / heartbeat 300s / double-start lock
echo          / stall watchdog 12min / backoff 30-60-120-300s
echo  stop   : python ma13_watchdog_main.py --stop
echo           (closing windows does NOT stop it any more)
echo ============================================================
REM ★ 2026-10-08fix: was calling sim_watchdog.py which does NOT exist here.
REMThe real watchdog is ma13_watchdog_main.py (same file ma13_start_now.cmd
REM   uses). That typo meant the 08:55 auto-start task silently did nothing.
REM * 2026-10-18: --symbols auto -> watchdog reads workbench.json selected.
REM * 2026-10-20: --detach -> watchdog survives window close (A2 fix).
"%PY%" -u "%~dp0ma13_watchdog_main.py" --symbols auto --trade --detach

echo.
echo [LAUNCHED] watchdog is running detached (no console of its own).
echo           Stop everything with: python ma13_watchdog_main.py --stop
timeout /t 8 >nul
endlocal
exit /b 0
