@echo off
chcp 65001 >nul
title 均线排列 回测(全部品种)
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY=python"
where python >nul 2>nul || set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
rem ---- locate project root: walk up until a dir containing 'lib' is found ----

set "ROOT="
call :_sub_findroot "%~dp0"

set "TEE=%ROOT%工具\tee_log.py"
set "LOGROOT=%ROOT%运行日志\02_均线排列\回测"
setlocal enabledelayedexpansion

echo ============================================================
echo  均线排列 回测：依次跑 甲醇 豆粕 螺纹钢 PVC 纯碱
echo  各品种日志写入 运行日志\02_均线排列\回测\[品种]\
echo ============================================================
echo.

for %%K in (MA M RB V SA) do (
  if /i "%%K"=="MA" set "NM=甲醇"
  if /i "%%K"=="M"  set "NM=豆粕"
  if /i "%%K"=="RB" set "NM=螺纹钢"
  if /i "%%K"=="V"  set "NM=PVC"
  if /i "%%K"=="SA" set "NM=纯碱"
  echo ------------------------------------------------------------
  echo  开始：!NM!
  echo ------------------------------------------------------------
  "%PY%" -u "!NM!\均线排列_回测.py" 2>&1 | "%PY%" "%TEE%" "%LOGROOT%\!NM!\回测日志.txt"
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
