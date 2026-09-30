@echo off
chcp 65001 >nul
title 焦煤 路线二_carry 模拟盘台账
cd /d "%~dp0"
for %%I in ("%~dp0..\..\..\..") do set "ROOT=%%~fI\"
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
echo  读取：实测记录\路线二_carry\焦煤\
"%PY%" "%ROOT%工具\模拟盘台账.py" JM 路线二_carry
echo  台账已刷新 -- 实测记录\路线二_carry\焦煤\焦煤_路线二_carry_台账.html
if exist "%ROOT%实测记录\路线二_carry\焦煤\焦煤_路线二_carry_台账.html" start "" "%ROOT%实测记录\路线二_carry\焦煤\焦煤_路线二_carry_台账.html"
pause

