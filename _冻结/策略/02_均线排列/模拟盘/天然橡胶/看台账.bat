@echo off
chcp 65001 >nul
title 天然橡胶 均线排列 模拟盘台账
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


echo ============================================================
echo  均线排列 天然橡胶 模拟盘台账(月度)
echo ============================================================
echo.
echo  读取：实测记录\02_均线排列\天然橡胶\天然橡胶_成交流水.csv + 运行心跳.csv
echo  输出：天然橡胶_均线排列_台账.html(覆盖更新，永远只有一份)
echo.

"%PY%" "%ROOT%工具\模拟盘台账.py" RU 均线排列

echo.
echo  台账已刷新 -- 实测记录\02_均线排列\天然橡胶\天然橡胶_均线排列_台账.html
if exist "%ROOT%实测记录\02_均线排列\天然橡胶\天然橡胶_均线排列_台账.html" start "" "%ROOT%实测记录\02_均线排列\天然橡胶\天然橡胶_均线排列_台账.html"
pause

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
