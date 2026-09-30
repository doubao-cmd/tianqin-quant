@echo off
chcp 65001 >nul
title 天勤量化 · 跑回测
set "PYTHONUTF8=1"

rem ---- locate project root: walk up until a dir containing 'lib' is found ----
set "ROOT="
call :_sub_findroot "%~dp0"

rem ---- 解释器：依次试到「真装了 tqsdk」的那个（PATH 里的 python 常常没装 tqsdk）----
rem ---- interpreter: pick the first candidate that really has tqsdk ----
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

if not exist "%ROOT%工具\菜单.py" (
  echo [错误] 找不到 工具\菜单.py，项目结构可能不完整。
  pause
  exit /b 1
)

cd /d "%ROOT%"
"%PY%" "%ROOT%工具\菜单.py"

echo.
echo ------------------------------------------------------------
echo  程序已结束（窗口可关闭）
echo ------------------------------------------------------------
pause
exit /b 0

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
