@echo off
REM ================================================================
REM MA13 simulator - STOP EVERYTHING (watchdog + core process tree)
REM
REM WHY THIS FILE EXISTS:
REM   Since the A2 fix (2026-10-20), the watchdog runs DETACHED.
REM   Closing its console window -- or the core's window -- is NOT a
REM   stop any more: the watchdog keeps running and restarts the core.
REM   The only real stop is:  python ma13_watchdog_main.py --stop
REM   This .cmd is just a double-click wrapper for that command.
REM
REM ASCII ONLY on purpose: cmd.exe swallows non-ASCII lines.
REM
REM WARNING:
REM   Stopping does NOT close positions. Once the process is gone,
REM   open positions have NO stop-loss being monitored. Check the
REM   TqKq app and the 台账 before you walk away.
REM
REM RELATED:
REM   Start  : ma13_start_now.cmd   (same folder)
REM   Health : python ma13_watchdog_main.py --hb-check
REM   Log    : 台账\守护日志.txt
REM ================================================================
setlocal
cd /d "%~dp0"

set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv python not found: %PY%
    pause
    exit /b 1
)

echo ============================================================
echo   STOP  MA13 simulator  (watchdog + core process tree)
echo ============================================================
echo.

"%PY%" ma13_watchdog_main.py --stop

echo.
echo ------------------------------------------------------------
echo  Stopped. Positions are NOT closed.
echo  If you are flat / it is out of trading hours, you are safe.
echo  If you hold positions, no stop-loss is monitored right now.
echo ------------------------------------------------------------
echo.
pause
exit /b 0
