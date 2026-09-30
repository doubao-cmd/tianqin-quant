@echo off
chcp 65001 >nul
title Rollback - code + summary report
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "PY=python"
where python >nul 2>nul || set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"

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
