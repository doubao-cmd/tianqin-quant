@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0..\..\.venv_tq\Scripts\python.exe"
echo [1/2] 拉取日线...
"%PY%" fetch_daily.py 400
echo.
echo [2/2] 扫描排列并推送...
"%PY%" scan_daily_push.py
echo.
pause
