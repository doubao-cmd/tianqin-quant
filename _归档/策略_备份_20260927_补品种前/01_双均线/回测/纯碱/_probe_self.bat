@echo off
chcp 65001 >nul
title 纯碱 双均线 回测
cd /d "%~dp0"
set "PYTHONUTF8=1"
echo ============================================================
echo ============================================================
set "PY=python"
set "ROOT="
call :_sub_findroot "%~dp0"
set "TEE=%ROOT%工具\tee_log.py"
set "ABS_LOG=%ROOT%运行日志\01_双均线\回测\纯碱\回测日志.txt"
pause
:_sub_findroot
set "_R=%~1"
set "_N=0"
:_sub_loop
if exist "%_R%lib" goto :_sub_found
set /a _N+=1
set "_P=!_R:~0,-1!"
for %%I in ("!_P!") do set "_UP=%%~dpI"
set "_R=!_UP!"
goto :_sub_loop
:_sub_found
endlocal & set "ROOT=%_R%"
goto :eof
echo PROBE_ROOT=[%ROOT%]
if exist "%ROOT%lib\engine.py" echo LIB_OK
