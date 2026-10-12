@echo off
chcp 65001 >nul
title CTP tick 录制（只连行情，无任何下单代码）
set "PYTHONUTF8=1"

rem ---- locate project root: walk up until a dir containing 'lib' is found ----
set "ROOT="
call :_sub_findroot "%~dp0"

rem ---- 解释器：依次试到「真装了 openctp-ctp 与 pyarrow」的那个 ----
set "PY="
set "_VENV_TQ=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
for %%P in ("%_VENV_TQ%" python py) do (
  if not defined PY (
    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if (u.find_spec('openctp_ctp') and u.find_spec('pyarrow') and u.find_spec('duckdb')) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)
if not defined PY (
  echo.
  echo  [错误] 没找到同时装了 openctp-ctp / pyarrow / duckdb 的 Python。
  echo         处理：在项目 venv 里执行
  echo           pip install openctp-ctp==6.3.15.* pyarrow duckdb -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo.
  pause
  exit /b 1
)

if not exist "%ROOT%工具\_CTP接入\simnow_account.py" (
  echo.
  echo  [错误] 缺少 工具\_CTP接入\simnow_account.py（把模板复制一份并填入 PASSWORD）。
  echo.
  pause
  exit /b 1
)

cd /d "%ROOT%"

echo ============================================================
echo   CTP tick 录制（SimNow）
echo   ★ 只订阅行情，脚本里没有任何下单代码。
echo.
echo   默认：录 5 分钟、每 300 秒落一次盘、按当前日期自动生成活跃合约。
echo   想录更久 / 指定合约，请从命令行跑：
echo     python 工具\_CTP接入\tick录制.py --minutes 30 --inst rb2701,m2701
echo   录完用：
echo     python 工具\_CTP接入\tick查询.py
echo ============================================================
echo.

"%PY%" "%ROOT%工具\_CTP接入\tick录制.py" %*

echo.
echo ------------------------------------------------------------
echo  录制结束（窗口可关闭）
echo ------------------------------------------------------------
pause
exit /b 0

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
