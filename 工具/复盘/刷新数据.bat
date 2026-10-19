@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"

rem ============================================================
rem  复盘终端 每日刷新：拉最新K线 -> 重建 bundle -> 重建单文件
rem  VIZ = 画图/构建环境（需要 pandas / numpy / plotly）
rem  TQ  = 天勤环境（需要 tqsdk，只有它能联网拉数据）
rem  ★ 两个路径都必须上溯两级：工具\复盘 -> 天勤量化
rem ============================================================
set "VIZ=%~dp0..\..\.venv_viz\Scripts\python.exe"
if not exist "%VIZ%" (
  echo [ERROR] 找不到 %VIZ%
  echo         构建脚本需要 pandas/numpy/plotly，请先创建该虚拟环境。
  popd & exit /b 1
)

set "TQ="
if exist "%~dp0..\..\envs\tqsdk\Scripts\python.exe" set "TQ=%~dp0..\..\envs\tqsdk\Scripts\python.exe"
if not defined TQ if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe" set "TQ=%USERPROFILE%\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe"

echo [1/3] 拉取最新K线（收盘后运行才有意义）...
rem ★★ 2026-10-18 改：拉取改走 exec4\daily_update.py —— 它在 data.py --fetch2026 之外
rem    多做三件事：①并入 _缓存\kline_archive（只追加不覆盖，修掉"每天少一天历史"）
rem    ②生成池层筛选表 ③写 _缓存\pool\_last_update.json（工具新鲜度角标读它）。
rem    ★ 失败不再"静默继续"：daily_update 会打红字 + 写 _FAILED_{date}.txt + 退出码非0。
rem      这里仍然允许继续构建（用旧缓存，工具照样能看），但必须把失败喊出来。
if defined TQ (
  "%TQ%" "%~dp0exec4\daily_update.py"
  if errorlevel 1 (
    echo.
    echo ============================================================
    echo  [警告] 数据更新失败！本次构建用的是【旧缓存】。
    echo         详情见  _缓存\pool\_FAILED_*.txt
    echo         常见原因：回测账号被别的进程占用（同一账号只能一处登录）
    echo ============================================================
    echo.
  )
) else (
  echo [警告] 未找到 tqsdk 环境，跳过拉取，改用本地缓存
)

echo [2/3] 重建 bundle...
"%VIZ%" "%~dp0build\build_data.py"
if errorlevel 1 (
  echo [ERROR] build_data.py 失败，中止（未重建单文件，旧文件仍可用）
  popd & exit /b 1
)

echo [3/3] 重建单文件...
"%VIZ%" "%~dp0build\build_html.py"
if errorlevel 1 (
  echo [ERROR] build_html.py 失败
  popd & exit /b 1
)

echo.
echo 完成。双击「复盘终端.html」或桌面快捷方式查看。
popd
endlocal
