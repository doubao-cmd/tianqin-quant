@echo off
chcp 65001 >nul
title 纯碱 布林带 模拟盘
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

set "TEE=%ROOT%工具\tee_log.py"
set "ABS_LOG=%ROOT%运行日志\03_布林带\模拟盘\纯碱\模拟盘日志.txt"

echo ============================================================
echo  布林带 纯碱 模拟盘(快期模拟账户)
echo ============================================================
echo.
echo  启动中... 浏览器会自动弹出面板
echo  信号会直接打印在下方黑窗口，无需打开日志
echo  (按 Ctrl+C 停止；直接关闭窗口也会结束程序)
echo.

:: 浏览器面板由 Python 端自动打开(见 lib/engine.py 的 run_live_boll)
:: 若未自动弹出，复制黑窗口里 [浏览器监控面板] 后的地址手动打开

:: 前台运行：输出同时显示在黑窗口 + 写入日志文件
"%PY%" -u 布林带_模拟盘.py 2>&1 | "%PY%" "%TEE%" "%ABS_LOG%"

echo.
echo  程序已退出(若非正常结束，请查看日志文件)
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
