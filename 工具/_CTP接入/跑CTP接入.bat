@echo off
chcp 65001 >nul
title CTP / SimNow 最小接入（默认只连行情，不下单）
set "PYTHONUTF8=1"

rem ---- locate project root: walk up until a dir containing 'lib' is found ----
set "ROOT="
call :_sub_findroot "%~dp0"

rem ---- 解释器：依次试到「真装了 openctp-ctp」的那个 ----
set "PY="
set "_VENV_TQ=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
for %%P in ("%_VENV_TQ%" python py) do (
  if not defined PY (
    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('openctp_ctp') else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)
if not defined PY (
  echo.
  echo  [错误] 没找到装了 openctp-ctp 的 Python，程序起不来。
  echo         已依次试过：%_VENV_TQ%  /  python  /  py
  echo         处理：在项目 venv 里执行
  echo           pip install openctp-ctp==6.3.15.* -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo.
  pause
  exit /b 1
)

rem ---- 账号文件检查（密码为空就别启动了） ----
if not exist "%ROOT%工具\_CTP接入\simnow_account.py" (
  echo.
  echo  [错误] 缺少 工具\_CTP接入\simnow_account.py
  echo         处理：把 simnow_account_template.py 复制一份改名，并填入 PASSWORD。
  echo.
  pause
  exit /b 1
)

cd /d "%ROOT%"

echo ============================================================
echo   CTP / SimNow 最小接入
echo   ★ 本次只连「行情」，不会下任何单。
echo.
echo   要连交易链路（只读查资金/持仓），请改用命令行加 --td
echo   要做下单往返测试，请加 --td --order --yes-i-know（真的报单）
echo ============================================================
echo.

"%PY%" "%ROOT%工具\_CTP接入\simnow_最小接入.py" %*

echo.
echo ------------------------------------------------------------
echo  程序已结束（窗口可关闭）
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
