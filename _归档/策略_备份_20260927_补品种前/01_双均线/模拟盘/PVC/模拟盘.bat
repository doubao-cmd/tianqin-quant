@echo off
chcp 65001 >nul
title PVC 双均线 模拟盘
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY=python"
where python >nul 2>nul || set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
rem ---- locate project root: walk up until a dir containing 'lib' is found ----

set "ROOT="
call :_sub_findroot "%~dp0"

set "TEE=%ROOT%工具\tee_log.py"
set "ABS_LOG=%ROOT%运行日志\01_双均线\模拟盘\PVC\模拟盘日志.txt"

REM [anti-dup] If this script's python process is already running (manual or scheduled), exit now.
REM Scheduler IgnoreNew only tracks its OWN instances, not manual double-clicks; this covers both.
set "ALREADY=NONE"
for /f "delims=" %%i in ('powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand JABwACAAPQAgAEcAZQB0AC0AQwBpAG0ASQBuAHMAdABhAG4AYwBlACAAVwBpAG4AMwAyAF8AUAByAG8AYwBlAHMAcwAgAHwAIABXAGgAZQByAGUALQBPAGIAagBlAGMAdAAgAHsAIAAkAF8ALgBOAGEAbQBlACAALQBlAHEAIAAnAHAAeQB0AGgAbwBuAC4AZQB4AGUAJwAgAC0AYQBuAGQAIAAkAF8ALgBDAG8AbQBtAGEAbgBkAEwAaQBuAGUAIAAtAGwAaQBrAGUAIAAnACoAzFNHV79+XwAhat9i2HYuAHAAeQAqACcAIAB9AAoAaQBmACAAKAAkAHAAKQAgAHsAIABXAHIAaQB0AGUALQBPAHUAdABwAHUAdAAgACcAUgBVAE4ATgBJAE4ARwAnACAAfQAgAGUAbABzAGUAIAB7ACAAVwByAGkAdABlAC0ATwB1AHQAcAB1AHQAIAAnAE4ATwBOAEUAJwAgAH0A') do set "ALREADY=%%i"
if "%ALREADY%"=="RUNNING" (
    echo [防双开] 检测到 双均线_模拟盘.py 已在运行，本窗口直接退出(避免重复下单)。
    if "%~1"=="" pause
    exit /b 0
)

echo ============================================================
echo  双均线 PVC 模拟盘(快期模拟账户)
echo ============================================================
echo.
echo  启动中... 浏览器会自动弹出面板
echo  信号会直接打印在下方黑窗口，无需打开日志
echo  (按 Ctrl+C 停止；直接关闭窗口也会结束程序)
echo.

:: 浏览器面板由 Python 端自动打开(见 lib/engine.py 的 run_live)
:: 若未自动弹出，复制黑窗口里 [浏览器监控面板] 后的地址手动打开

:: 前台运行：输出同时显示在黑窗口 + 写入日志文件
"%PY%" -u 双均线_模拟盘.py 2>&1 | "%PY%" "%TEE%" "%ABS_LOG%" utf-8 a

echo.
echo  程序已退出(若非正常结束，请查看日志文件)

:: 计划任务调用会传 auto：python 一退出就收工，不让任务卡在 Running。
:: 手动双击(无参数)则停下来，方便看报错。
if "%~1"=="" pause

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
