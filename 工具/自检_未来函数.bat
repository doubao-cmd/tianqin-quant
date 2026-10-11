@echo off
chcp 65001 >nul
title 防未来函数体检 · 规则 1

rem ---- locate project root: walk up until a dir containing 'lib' is found ----
set "ROOT="
call :_sub_findroot "%~dp0"

rem ---- interpreter: pick the first candidate that really has numpy ----
set "PY="
set "_VENV_TQ=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
for %%P in ("%_VENV_TQ%" python py) do (
  if not defined PY (
    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('numpy') else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)
if not defined PY (
  echo.
  echo  [ERROR] no python with numpy found.
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo  no-lookahead check  (rule 1)
echo ============================================================
"%PY%" "%ROOT%工具\自检_未来函数.py" %*
echo.
echo ------------------------------------------------------------
echo  exit code 0 = clean / 1 = found problems
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
