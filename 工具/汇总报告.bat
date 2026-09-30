@echo off
chcp 65001 >nul
title PVC 双均线 修改记录汇总(刷新)
cd /d "%~dp0.."
set "PYTHONUTF8=1"

echo ============================================================
echo  PVC 双均线 · 修改记录汇总报告
echo ============================================================
echo.
echo  作用：把最新一次回测的数字抓进汇总页，并重新渲染
echo  文字说明(改了什么 / 为什么)：工具\_账本\PVC_双均线.json
echo  输出：回测结果\01_双均线\PVC\PVC_双均线_汇总.html
echo.
echo  提示：正常跑回测(回测.bat)结束时会自动刷新，
echo        这个 bat 只是“手动补刷新”用的。
echo.

rem ---- interpreter: pick the first candidate that really has tqsdk ----
rem      (the python on PATH is often another install without tqsdk)
set "PY="
set "_VENV_TQ=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
for %%P in ("%_VENV_TQ%" python py) do (
  if not defined PY (
    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('tqsdk') else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)
if not defined PY (
  echo.
  echo  [错误] 没找到装了 tqsdk 的 Python，程序起不来。
  echo         已依次试过：%_VENV_TQ%  /  python  /  py
  echo         处理：确认天勤 tqsdk 虚拟环境还在；不在就重装一次 tqsdk。
  echo.
  pause
  exit /b 1
)

"%PY%" -u "工具\汇总报告.py" V 双均线
if errorlevel 1 goto fail

echo.
echo ------------------------------------------------------------
echo  完成，正在打开汇总报告 ...
echo ------------------------------------------------------------
start "" "回测结果\01_双均线\PVC\PVC_双均线_汇总.html"
pause
exit /b 0

:fail
echo.
echo !! 刷新失败，请检查上面的报错信息(多半是还没跑过回测)
pause
exit /b 1
