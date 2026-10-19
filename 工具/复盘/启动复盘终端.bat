@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ★ 上溯两级才到 天勤量化\.venv_viz（原来少了一级，会退化成裸 python）
set "PY=%~dp0..\..\.venv_viz\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo 复盘终端（HTTP 模式）启动中... 浏览器打开 http://127.0.0.1:8131
"%PY%" serve.py
popd
pause
