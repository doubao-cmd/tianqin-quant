@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"

rem ============================================================
rem  四期 S4 · 每日数据更新（不含构建，专供计划任务调用）
rem
rem  与「刷新数据.bat」的区别：
rem    刷新数据.bat      = 拉数据 + 重建 bundle + 重建单文件（你手动看之前跑）
rem    每日数据更新.bat  = 只拉数据 + 并入 archive + 池层表（计划任务跑）
rem  → 构建很重（约 30s+），每天跑三次没必要；数据才是要持续攒的。
rem
rem  计划任务（沙箱里建不了，请手动执行一次 cmd）：
rem    schtasks /Create /TN "MA13数据更新_早" /TR "cmd /c \"C:\Users\cc\Desktop\天勤量化\工具\复盘\每日数据更新.bat\"" /SC DAILY /ST 11:35 /F
rem    schtasks /Create /TN "MA13数据更新_午" /TR "cmd /c \"C:\Users\cc\Desktop\天勤量化\工具\复盘\每日数据更新.bat\"" /SC DAILY /ST 15:05 /F
rem    schtasks /Create /TN "MA13数据更新_夜" /TR "cmd /c \"C:\Users\cc\Desktop\天勤量化\工具\复盘\每日数据更新.bat\"" /SC DAILY /ST 23:05 /F
rem
rem  ★ 三个时点来自方案 §2.6：早盘收(11:35) / 日盘收(15:05) / 夜盘收(23:05)
rem  ★ 账号：本脚本用【主账号=回测号】，不会抢模拟盘那个账号（config_account.check()）
rem ============================================================

set "TQ="
if exist "%~dp0..\..\envs\tqsdk\Scripts\python.exe" set "TQ=%~dp0..\..\envs\tqsdk\Scripts\python.exe"
if not defined TQ if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe" set "TQ=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"

if not defined TQ (
  echo [ERROR] 找不到带 tqsdk 的 python，无法联网拉数据。
  echo         期望路径之一：
  echo           %USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe
  popd & exit /b 1
)

"%TQ%" "%~dp0exec4\daily_update.py"
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
  echo.
  echo ============================================================
  echo  [失败] 数据更新未成功  ^(退出码 %RC%^)
  echo         标记文件： _缓存\pool\_FAILED_*.txt
  echo         最常见原因：回测/研发账号被别的进程占着
  echo         ^(同一账号同时只能一处登录^)
  echo ============================================================
  echo.
)

popd
endlocal & exit /b %RC%
