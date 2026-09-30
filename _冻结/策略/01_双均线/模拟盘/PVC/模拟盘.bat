@echo off
chcp 65001 >nul
title PVC 双均线 模拟盘
cd /d "%~dp0"
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
  echo  [错误] 没找到装了 tqsdk 的 Python，程序起不来。
  echo         已依次试过：%_VENV_TQ%  /  python  /  py
  echo         处理：确认天勤 tqsdk 虚拟环境还在；不在就重装一次 tqsdk。
  echo.
  pause
  exit /b 1
)
rem ---- locate project root: walk up until a dir containing 'lib' is found ----

set "ROOT="
call :_sub_findroot "%~dp0"


REM [anti-dup] If this script's python process is already running (manual or scheduled), exit now.
REM Scheduler IgnoreNew only tracks its OWN instances, not manual double-clicks; this covers both.
set "ALREADY=NONE"
for /f "delims=" %%i in ('powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand JABwACAAPQAgAEcAZQB0AC0AQwBpAG0ASQBuAHMAdABhAG4AYwBlACAAVwBpAG4AMwAyAF8AUAByAG8AYwBlAHMAcwAgAHwAIABXAGgAZQByAGUALQBPAGIAagBlAGMAdAAgAHsAIAAkAF8ALgBOAGEAbQBlACAALQBlAHEAIAAnAHAAeQB0AGgAbwBuAC4AZQB4AGUAJwAgAC0AYQBuAGQAIAAkAF8ALgBDAG8AbQBtAGEAbgBkAEwAaQBuAGUAIAAtAGwAaQBrAGUAIAAnACoAzFNHV79+XwAhat9i2HYuAHAAeQAqACcAIAB9AAoAaQBmACAAKAAkAHAAKQAgAHsAIABXAHIAaQB0AGUALQBPAHUAdABwAHUAdAAgACcAUgBVAE4ATgBJAE4ARwAnACAAfQAgAGUAbABzAGUAIAB7ACAAVwByAGkAdABlAC0ATwB1AHQAcAB1AHQAIAAnAE4ATwBOAEUAJwAgAH0A') do set "ALREADY=%%i"
if "%ALREADY%"=="RUNNING" (
    echo [防双开] 检测到 双均线_模拟盘.py 已在运行，本窗口直接退出（避免重复下单）。
    if "%~1"=="" pause
    exit /b 0
)

echo ============================================================
echo  双均线 PVC 模拟盘(快期模拟账户)
echo ============================================================
echo.
echo  启动中... 浏览器会自动弹出面板
echo  信号会直接打印在下方黑窗口，无需打开日志
echo  (按 Ctrl+C 停止；直接关闭窗口也会结束程序)
echo.

rem  Browser panel is opened by the Python side (see lib/engine.py run_live)
rem  If it does not pop up, copy the URL printed after [browser panel]

rem  Run in foreground: output goes straight to this window
rem ---- entry script: take it by wildcard so this line stays pure ASCII ----
rem      (a non-ASCII filename written right here gets eaten by cmd after chcp 65001)
set "_ENTRY="
for %%F in (*.py) do if not defined _ENTRY set "_ENTRY=%%F"
if not defined _ENTRY (
  echo  [ERROR] no .py script found in this folder
  pause
  exit /b 1
)
"%PY%" -u "%_ENTRY%"

echo.
echo  程序已退出

rem  Scheduler calls this with "auto": exit as soon as python returns
rem  Manual double-click (no arg): stay open so errors remain visible
if "%~1"=="" pause

:_sub_findroot
setlocal enabledelayedexpansion
set "_R=%~1"
set "_N=0"
:_sub_loop
if exist "%_R%lib" goto :_sub_found
set /a _N+=1
if !_N! GTR 12 goto :_sub_found
set "_P=!_R:~0,-1!"
for %%I in ("!_P!") do set "_UP=%%~dpI"
if "!_UP!"=="" goto :_sub_found
set "_R=!_UP!"
goto :_sub_loop
:_sub_found
endlocal & set "ROOT=%_R%"
goto :eof
