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

REM ---- 5 legs, picked 2026-10-08 by correlation over IS2016-2019 ----
REM   JM must stay (user). JM + M + JD + P + SR has the LOWEST worst-pair
REM   |rho| = 0.292 of all 70 possible combos (criterion = min max-pair,
REM   NOT min average: what actually hurts live is one pair moving together).
REM   Full matrix: _收纳/输出_20261008/选5腿结论_20261008.md
REM     (2026-10-08 the reports were moved into _收纳/ by the dir cleanup,
REM      so the older path 工具/_输出/... no longer exists)
REM   ⚠️ HAND_MAX=8 caps every leg, so RISK_PCT=1% never binds and the 5
REM      legs are NOT equal-risk (JM ~35.6%/yr vs SR ~13.5%/yr). Known, not fixed.
echo [START] MA13 simulator launching (JM + M + JD + P + SR, TqKq cloud sim)...
"%PY%" -u "%~dp0ma13_watchdog_main.py" --symbols JM,M,JD,P,SR --trade

echo.
echo [EXIT] simulator stopped.
exit /b 0