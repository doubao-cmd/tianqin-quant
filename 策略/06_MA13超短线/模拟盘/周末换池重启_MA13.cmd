@echo off
REM ================================================================
REM MA13 - weekend pool swap + restart (ONE CLICK)
REM
REM What this does:
REM   1) stop the running watchdog + core, and WAIT until the old PID
REM      is really gone (aborts instead of running twice)
REM   2) re-select the pool in the FOREGROUND, so the log is visible
REM   3) relaunch the watchdog detached (this window closes itself)
REM
REM When to run: Saturday / Sunday any time, or Monday before 09:00.
REM Running it twice in the same weekend is SAFE: the 2nd run sees
REM gap < 6 days and keeps the same pool.
REM
REM Do NOT run it in the middle of a trading session unless you mean
REM it - it stops and restarts the core (positions are taken over by
REM the _carried protection, stop-loss lines are rebuilt).
REM
REM NOTE: this file is ASCII-only on purpose. Keep it that way.
REM ================================================================
setlocal
cd /d "%~dp0"

set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] tqsdk venv python not found:
    echo         %PY%
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   MA13 weekend pool swap
echo ============================================================
echo.

"%PY%" -u "%~dp0ma13_watchdog_main.py" --restart --symbols auto --trade
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo ============================================================
    echo   [FAILED] rc=%RC%  -- restart aborted, nothing was started
    echo   Check the watchdog log before the next session opens.
    echo ============================================================
    pause
    exit /b %RC%
)

echo ============================================================
echo   [OK] new pool is live, watchdog relaunched detached.
echo   Log: ledger folder, the watchdog log txt file
echo   This window closes in 8 seconds.
echo ============================================================
"%SystemRoot%\System32\timeout.exe" /t 8 /nobreak >nul
exit /b 0
