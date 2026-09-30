@echo off
chcp 65001 >nul
cd /d %~dp0..\..\..\..
echo ============================================
echo  MA13 模拟盘 - 棕榈油(P)+苯乙烯(EB) 双品种
echo  账号: 模拟盘专用  模式: 观察模式(不报单)
echo  双层守护: 断线自动重连 + 崩溃自动重启
echo  停止: 关闭本窗口 或 按 Ctrl+C
echo ============================================
C:/Users/cc/.workbuddy/binaries/python/versions/3.13.12/python.exe -u "策略/06_MA13超短线/模拟盘/模拟盘守护.py" --symbols P,EB
pause
