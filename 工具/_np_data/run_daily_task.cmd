@echo off
REM ============================================================
REM np_data daily fetch wrapper (ASCII only by design)
REM   Scheduled Task : np_data_daily, trigger 17:10 Mon-Fri
REM   Manual run     : double-click this file
REM   Single date    : python run_daily.py 20261022
REM   Log            : _daily_log.txt (append, one block per run)
REM WHY ASCII: any Chinese byte in a .cmd gets mangled by cmd.exe
REM   line-by-line (verified 2026-10-07 on this machine).
REM WHY 17:10: exchange receipt / rank files are usually published
REM   between 15:30 and 16:30. 15:42 attempt failed with
REM   "Excel file format cannot be determined" = file not up yet.
REM ============================================================
setlocal
cd /d "%~dp0"
set PY=C:\Users\cc\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe
echo [%date% %time%] ==== start ==== >> _daily_log.txt
"%PY%" run_daily.py >> _daily_log.txt 2>&1
echo [%date% %time%] ==== exit=%errorlevel% ==== >> _daily_log.txt
endlocal
