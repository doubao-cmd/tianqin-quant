@echo off
chcp 65001 >nul
title 均线排列 回测(全部 12 品种)
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

setlocal enabledelayedexpansion

echo ============================================================
echo  均线排列 回测：依次跑 12 个品种
echo  信号直接打印在下方黑窗口，不写日志文件
echo ============================================================
echo.

for %K in (V MA SA RB M EB JD JM LH P RU SR) do (
  if /i "%K"=="V"    set "NM=PVC"
  if /i "%K"=="MA"   set "NM=甲醇"
  if /i "%K"=="SA"   set "NM=纯碱"
  if /i "%K"=="RB"   set "NM=螺纹钢"
  if /i "%K"=="M"    set "NM=豆粕"
  if /i "%K"=="EB"   set "NM=苯乙烯"
  if /i "%K"=="JD"   set "NM=鸡蛋"
  if /i "%K"=="JM"   set "NM=焦煤"
  if /i "%K"=="LH"   set "NM=生猪"
  if /i "%K"=="P"    set "NM=棕榈油"
  if /i "%K"=="RU"   set "NM=天然橡胶"
  if /i "%K"=="SR"   set "NM=白糖"
  echo ------------------------------------------------------------
  echo  开始：!NM!
  echo ------------------------------------------------------------
  "%PY%" -u "!NM!\均线排列_回测.py"
  echo  !NM! 完成
  echo.
)
echo ============================================================
echo  全部完成。报告在 回测结果/02_均线排列/ 各品种目录
echo ============================================================
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
