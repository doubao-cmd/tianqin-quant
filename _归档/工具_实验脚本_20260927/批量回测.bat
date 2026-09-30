@echo off
chcp 65001 >nul
title 批量回测(01双均线 + 02均线排列 × 5品种)
cd /d "%~dp0.."
set "PYTHONUTF8=1"

echo ============================================================
echo  批量回测：2 策略 x 5 品种
echo ============================================================
echo.
echo  默认跑「练习区间 2026-03-13 ~ 09-13」(样本内，可反复跑)
echo  要跑期末考试：先把 工具\批量回测.py 里的 SAMPLE 改成 "out"
echo  报告输出到 回测结果\ 下按(策略\品种)两层分目录，日志在 运行日志\批量_回测.log
echo.
echo  全程约 30~40 分钟(PVC 那个通常最慢，其余每个约 2 分钟)
echo.

set "PY=python"
where python >nul 2>nul || set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"
set "TEE=工具\tee_log.py"
set "ABS_LOG=%CD%\运行日志\批量_回测.log"

"%PY%" -u "工具\批量回测.py" 2>&1 | "%PY%" "%TEE%" "%ABS_LOG%"

echo.
echo ------------------------------------------------------------
echo  批量回测已结束(若非正常结束，请查看 运行日志\批量_回测.log)
echo ------------------------------------------------------------
pause
