@echo off
chcp 65001 >nul
title 天勤量化 模拟盘自检
cd /d "%~dp0"

rem ---- locate project root: walk up until a dir containing 'lib' is found ----

set "ROOT="
call :_sub_findroot "%~dp0"


rem ---- 解释器：优先 PATH 里的 python，找不到才回退到本机 venv ----
set "PY=python"
where python >nul 2>nul || set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"

echo ============================================================
echo  天勤量化 模拟盘自检
echo ------------------------------------------------------------
echo  逐个跑一遍 5 策略 x 12 品种 = 60 个模拟盘入口
echo    · 静态：语法 / 品种代号 / 参数 / 防误下单开关 / bat
echo    · 动态：真连天勤，确认进主循环并落下运行心跳
echo.
echo  全程只观察、不下单；自检用的临时文件跑完自动进回收站
echo  60 个入口大约 8~12 分钟，中途别关这个窗口
echo ============================================================
echo.
"%PY%" -u "%ROOT%工具\模拟盘自检.py"
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
