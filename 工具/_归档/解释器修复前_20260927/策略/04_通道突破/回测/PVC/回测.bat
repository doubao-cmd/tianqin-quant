@echo off
chcp 65001 >nul
title PVC 通道突破 回测
cd /d "%~dp0"
set "PYTHONUTF8=1"

echo ============================================================
echo  通道突破 PVC 回测
echo ============================================================
echo.
echo  回测运行中... 信号会直接打印在下方黑窗口，无需打开日志
echo  跑完自动弹出 HTML 报告(也存到 回测结果/04_通道突破/PVC/)
echo.

set "PY=python"
where python >nul 2>nul || set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
rem ---- locate project root: walk up until a dir containing 'lib' is found ----

set "ROOT="
call :_sub_findroot "%~dp0"


"%PY%" -u 通道突破_回测.py

echo.
echo ------------------------------------------------------------
echo  程序已结束
echo ------------------------------------------------------------
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
