@echo off
chcp 65001 >nul
title 螺纹钢 路线二carry 模拟盘
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY="
set "_VENV_TQ=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
for %%P in ("%_VENV_TQ%" python py) do (
  if not defined PY (
    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('tqsdk') else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)
if not defined PY (
  echo  [错误] 没找到装了 tqsdk 的 Python。
  pause
  exit /b 1
)
echo ============================================================
echo  路线二carry 螺纹钢 模拟盘(快期模拟账户 · 观察模式)
echo ============================================================
echo  启动中... 浏览器会自动弹出面板；Ctrl+C 停止
echo.
"%PY%" -u 跨期结构carry_模拟盘.py
echo.
echo  程序已退出
pause

