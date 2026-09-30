# -*- coding: utf-8 -*-
# ⚠️ 本文件由模板机械生成（2026-09-29 模拟盘同步补齐）。品种相关仅 SYMBOL_KEY/名称/记录路径。
#    核心信号逻辑在 ../ma13_live_core.py（与回测脚本逐条对齐），默认观察模式。
"""MA13 超短线 —— 模拟盘入口（PVC）"""
import sys, os
_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "策略", "06_MA13超短线", "模拟盘"))

from ma13_live_core import run_live_ma13

SYMBOL_KEY = "V"
ALLOW_TRADE = False      # [!] 先观察：False = 只打印信号+写心跳，不报单；确认无误改 True
RECORD_DIR = os.path.join(_ROOT, "实测记录", "06_MA13超短线", "PVC")

if __name__ == "__main__":
    run_live_ma13(SYMBOL_KEY, allow_trade=ALLOW_TRADE, web_gui=True,
                  record_dir=RECORD_DIR, heartbeat_sec=300)
