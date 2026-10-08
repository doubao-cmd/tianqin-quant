@echo off
chcp 65001 >nul
title MA13 SIM P+EB+JM
set "PYTHONUTF8=1"

rem ============================================================
rem  CRITICAL: do NOT hardcode any non-ASCII path in this file.
rem  Project root is C:\Users\cc\Desktop\[CN]\[CN]\strategy\06_...\sim
rem  After "chcp 65001" cmd reads bytes as UTF-8 while %~dp0 hands
rem  back ANSI/GBK, so any CN segment gets silently mangled and the
rem  resolved path collapses to a parent folder. Work with the
rem  script's own directory (ASCII-safe "%~dp0" only) and locate
rem  files by ASCII name via "for %%F in (*.py)".
rem ============================================================
cd /d "%~dp0"

rem ---- locate watchdog by ASCII filename (no CN literal needed) ----
set "WATCHDOG="
for %%F in (*.py) do (
  if /i "%%~nxF"=="ma13_watchdog_main.py" set "WATCHDOG=%%~fF"
)
if not defined WATCHDOG (
  echo  [ERROR] ma13_watchdog_main.py not found next to this bat.
  pause
  exit /b 1
)

rem ---- project root = 4 levels up from this script ----
set "ROOT="
for %%I in ("..\..\..\..") do set "ROOT=%%~fI"

rem ---- interpreter: MUST be the venv python (tqsdk lives there only).
rem The system Python312 has NO tqsdk: it used to be picked as a fallback and
rem died silently as a 1-thread / 0-CPU / no-socket shell process.
rem Fail loudly instead of falling through to a broken interpreter.
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
if not exist "%PY%" (
  echo  [ERROR] tqsdk venv python not found: %PY%
  pause
  exit /b 1
)
"%PY%" -c "import importlib.util as u,sys; sys.exit(0 if u.find_spec('tqsdk') else 1)" >nul 2>nul
if errorlevel 1 (
  echo  [ERROR] '%PY%' cannot import tqsdk - refusing to start.
  pause
  exit /b 1
)

echo ============================================
echo  MA13 SIM - P(palm) + EB + JM, real order
echo  account: TqKq cloud sim (visible in phone app)
echo  watchdog: %WATCHDOG%
echo  stop: close this window or press Ctrl+C
echo ============================================
cd /d "%ROOT%"
"%PY%" -u "%WATCHDOG%" --symbols P,EB,JM --trade

echo.
echo ------------------------------------------------------------
echo  SIM exited (window can be closed)
echo ------------------------------------------------------------
pause