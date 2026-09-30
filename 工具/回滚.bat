@echo off
chcp 65001 >nul
title Rollback - code + summary report
cd /d "%~dp0.."
set "PYTHONUTF8=1"
rem ---- interpreter: pick the first candidate that really has tqsdk ----
rem      (the python on PATH is often another install without tqsdk)
set "PY="
set "_VENV_TQ=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
for %%P in ("%_VENV_TQ%" python py) do (
  if not defined PY (
    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('tqsdk') else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)
if not defined PY (
  echo.
  echo  [ERROR] No Python with tqsdk found. Tried:
  echo          %_VENV_TQ%
  echo          python  /  py
  echo.
  pause
  exit /b 1
)

rem ==================================================================
rem  ASCII ONLY. Do NOT put Chinese text in this file, not even in rem.
rem  Reason: while cmd re-parses this file after chcp 65001, a line
rem  with an odd number of Chinese bytes can swallow its own CR and
rem  merge with the next line. Observed damage: the launcher line
rem  "%PY%" -u "tools\rollback.py" got cut to 'u ...' -> "u is not
rem  recognized". All Chinese guidance is printed by 工具\回滚.py.
rem ==================================================================
"%PY%" -u "工具\回滚.py"
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
