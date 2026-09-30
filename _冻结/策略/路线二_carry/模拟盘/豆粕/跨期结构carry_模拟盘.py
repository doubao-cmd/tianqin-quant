# -*- coding: utf-8 -*-
# ⚠️ 本文件由模板机械生成（2026-09-29 模拟盘同步补齐）。品种相关仅 SYMBOL_KEY/名称/活跃月/记录路径。
#    核心逻辑在 ../carry_live_core.py（与回测滚动版逐条对齐），默认观察模式。
"""路线二 carry —— 模拟盘入口（豆粕）"""
import sys, os
_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "策略", "路线二_carry", "模拟盘"))

from carry_live_core import run_live_carry

SYMBOL_KEY = "M"
ACT_MONTHS = [1, 5, 9]       # 活跃月序列（与回测脚本一致）
ALLOW_TRADE = False      # [!] 先观察：False = 每日收盘打印结构+写心跳，不报单
RECORD_DIR = os.path.join(_ROOT, "实测记录", "路线二_carry", "豆粕")

if __name__ == "__main__":
    run_live_carry(SYMBOL_KEY, ACT_MONTHS, allow_trade=ALLOW_TRADE, web_gui=True,
                   record_dir=RECORD_DIR, heartbeat_sec=300)
