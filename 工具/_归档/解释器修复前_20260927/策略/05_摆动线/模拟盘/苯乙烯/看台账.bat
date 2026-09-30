@echo off
chcp 65001 >nul
title 苯乙烯 摆动线 模拟盘台账
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY=python"
where python >nul 2>nul || set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
rem ---- locate project root: walk up until a dir containing 'lib' is found ----

set "ROOT="
call :_sub_findroot "%~dp0"


echo ============================================================
echo  摆动线 苯乙烯 模拟盘台账(月度)
echo ============================================================
echo.
echo  读取：实测记录\05_摆动线\苯乙烯\苯乙烯_成交流水.csv + 运行心跳.csv
echo  输出：苯乙烯_摆动线_台账.html(覆盖更新，永远只有一份)
echo.

"%PY%" "%ROOT%工具\模拟盘台账.py" EB 摆动线

echo.
echo  台账已刷新 -- 实测记录\05_摆动线\苯乙烯\苯乙烯_摆动线_台账.html
if exist "%ROOT%实测记录\05_摆动线\苯乙烯\苯乙烯_摆动线_台账.html" start "" "%ROOT%实测记录\05_摆动线\苯乙烯\苯乙烯_摆动线_台账.html"
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
