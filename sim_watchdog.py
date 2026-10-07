# -*- coding: utf-8 -*-
"""ASCII-named entry for the MA13 simulator watchdog (project root).

Why this file exists
--------------------
The auto-start wrapper `策略/06_MA13超短线/模拟盘/每日0855自动启动.bat` must stay
**pure ASCII**: cmd.exe swallows Chinese lines after `chcp 65001` (verified
2026-10-07 on this machine). But the watchdog lives in a Chinese-named folder,
so the .bat cannot reference it directly. This shim resolves that path with
unicode escapes (kept ASCII on disk) and hands control to the watchdog via
runpy, so `__file__` inside the watchdog still points at its real location.

Usage (what the .bat runs):
    python sim_watchdog.py --symbols P,EB,JM --trade
"""
import os
import runpy

_HERE = os.path.dirname(os.path.abspath(__file__))

# 策略 / 06_MA13超短线 / 模拟盘 / 模拟盘守护.py  -- written as escapes to stay ASCII
_WATCHDOG = os.path.join(
    _HERE,
    "\u7b56\u7565",                                     # 策略
    "06_MA13\u8d85\u77ed\u7ebf",                        # 06_MA13超短线
    "\u6a21\u62df\u76d8",                               # 模拟盘
    "\u6a21\u62df\u76d8\u5b88\u62a4.py",                # 模拟盘守护.py
)

if not os.path.isfile(_WATCHDOG):
    raise SystemExit(f"[ERROR] watchdog not found: {_WATCHDOG}")

if __name__ == "__main__":
    runpy.run_path(_WATCHDOG, run_name="__main__")
