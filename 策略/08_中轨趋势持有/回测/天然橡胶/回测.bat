@echo off
chcp 65001 >nul
title 天然橡胶 中轨趋势持有 回测
cd /d "%~dp0"
set "PYTHONUTF8=1"

echo ============================================================
echo  中轨趋势持有(日线) 天然橡胶 回测
echo ============================================================
echo.
echo  回测运行中... 信号会直接打印在下方黑窗口
echo  跑完自动弹出 HTML 报告
echo.

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

"%PY%" -u 中轨趋势_回测.py

echo.
echo ------------------------------------------------------------
echo  程序已结束(若非正常结束，请向上翻查看报错)
echo ------------------------------------------------------------
pause
