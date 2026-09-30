@echo off
chcp 65001 >nul
title MA13 模拟盘 P+EB 观察模式
set "PYTHONUTF8=1"
set "ROOT="
for %%I in ("%~dp0..\..\..\..") do set "ROOT=%%~fI\"
cd /d "%ROOT%"

rem ---- 解释器：依次试到「真装了 tqsdk」的那个 ----
set "PY="
set "_VENV_TQ=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
for %%P in ("%_VENV_TQ%" python py) do (
  if not defined PY (
    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('tqsdk') else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)
if not defined PY (
  echo  [错误] 没找到装了 tqsdk 的 Python，程序起不来。
  pause
  exit /b 1
)

echo ============================================
echo  MA13 模拟盘 - 棕榈油(P)+苯乙烯(EB) 双品种
echo  账号: 模拟盘专用  模式: 观察模式(不报单)
echo  双层守护: 断线自动重连 + 崩溃自动重启
echo  停止: 关闭本窗口 或 按 Ctrl+C
echo ============================================
"%PY%" -u "%ROOT%策略\06_MA13超短线\模拟盘\模拟盘守护.py" --symbols P,EB
pause
