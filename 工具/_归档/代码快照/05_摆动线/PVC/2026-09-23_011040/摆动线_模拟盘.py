# -*- coding: utf-8 -*-
"""
摆动线策略 —— 模拟盘入口（30分钟 · 摆动点结构双向反手，零参数裸版）
==========================================================
逻辑与回测完全一致（swing_decision 同一状态机）。先观察、再真下单。
双向反手：收盘上穿最近确认摆动高点开多 / 收盘跌破最近确认摆动低点开空。
移动止盈线=持仓方向最近确认摆动点（只前进不后退）；出场与反手是同一事件。
"""
import sys
import os

# 把项目根目录（天勤量化/）加到搜索路径
_ROOT = os.path.dirname(os.path.abspath(__file__))  # 从脚本所在目录开始
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)  # 逐级向上，找到含 lib 的项目根目录为止
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ==================== 可调参数 ====================
SYMBOL_KEY = "V"
VOLUME = 1
KLINE_SEC = 30 * 60                    # 30 分钟 K 线（与回测同口径）
ALLOW_TRADE = False                   # [!] 先观察：False = 只打印信号不下单；确认无误改 True
WEB_GUI = True                        # 是否打开天勤 web_gui 面板（实时K线/信号/持仓/资金曲线）
# =================================================


if __name__ == "__main__":
    engine.run_live_swing(
        SYMBOL_KEY, volume=VOLUME,
        kline_sec=KLINE_SEC, allow_trade=ALLOW_TRADE,
        web_gui=WEB_GUI,
    )
