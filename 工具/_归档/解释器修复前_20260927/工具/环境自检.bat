@echo off
chcp 65001 >nul
title 天勤量化 环境自检
cd /d "%~dp0"

rem ---- locate project root: walk up until a dir containing 'lib' is found ----

rem ---- locate project root: walk up until a dir containing 'lib' is found ----

set "ROOT="
call :_sub_findroot "%~dp0"


rem ---- 解释器：优先 PATH 里的 python，找不到才回退到本机 venv ----
set "PY=python"
where python >nul 2>nul || set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"

echo ============================================================
echo  天勤量化 环境自检
echo ============================================================
echo.
"%PY%" "%ROOT%工具\环境自检.py"
echo.
echo ------------------------------------------------------------
echo  程序已结束(或按 Ctrl+C 中断)
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
