@echo off
REM 工作台单启动器（双击即可）
REM 切换品种：编辑 workbench.json 的 "selected"，或改用 run_workbench.py --select RB,MA,EG
REM 默认 --dry-run 仅打印计划；确认无误后去掉 --dry-run 并加 --allow-trade 才真实下单。
chcp 65001 >nul
setlocal
set PY=C:\Users\cc\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe
set DIR=%~dp0
"%PY%" "%DIR%run_workbench.py" --dry-run
echo.
echo 以上为计划预览。若要真实运行，编辑本文件把上一行 "--dry-run" 改为 "--allow-trade"。
pause
endlocal
