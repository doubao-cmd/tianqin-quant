# -*- coding: utf-8 -*-
# ⚠️ 本文件由「豆粕」版机械复制生成（2026-09-27 批量补品种，12 品种口径统一）。
#    品种相关处（SYMBOL_KEY / 报告路径 / 账本路径 / 滑点说明）已换成本品种；
#    docstring 里的版本沿革与实验数字记录的是**原品种「豆粕」**的过程，本品种未单独复现。
#    核心买卖逻辑在 lib/engine.py，各品种共用同一份，不会走偏。
"""
均线多空排列策略 —— 模拟盘入口（15分钟 · 多头排列 + MACD 水上金叉）
==========================================================
逻辑与回测完全一致（看 alignment_signal）。先观察、再真下单。
仅做多。出场只看 MACD 死叉，无止盈、无止损。
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
SYMBOL_KEY = "RU"
MA_PERIODS = (5, 10, 20, 40)          # 四根均线，判断多头排列（短->长）
VOLUME = 1
KLINE_SEC = 15 * 60                    # 15 分钟 K 线
ALLOW_TRADE = False                   # [!] 先观察：False = 只打印信号不下单；确认无误改 True
WEB_GUI = True             # 是否打开天勤 web_gui 面板（实时K线/信号/持仓/资金曲线）
STOP_PCT = 0.0                        # 百分比止损（0=关闭）

# ---- 运行记录（2026-09-27 补齐，与 01/03/04/05 一致）----
#   把「程序有没有在跑」和「有没有开仓条件」分开落盘，供事后做台账：
#     实测记录/02_均线排列/天然橡胶/天然橡胶_成交流水.csv  （事件流）
#     实测记录/02_均线排列/天然橡胶/天然橡胶_运行心跳.csv  （每 5 分钟一行，**休市照写**）
#   想关掉记录：把 RECORD_DIR 改成 None。
RECORD_DIR = os.path.join(_ROOT, "实测记录", "02_均线排列", "天然橡胶")
RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）

# ---- 账户流水单（2026-09-27 补齐，与其余策略一致）----
#   账户视角：不管哪个策略、哪个品种，账户里所有真实成交都记进这里。
#   与上面「策略视角」的 _成交流水.csv 互补，互不替代。
LEDGER_DIR = os.path.join(_ROOT, "实测记录", "账户")
# =================================================


alignment_signal = engine.align_signal_long_macd  # 共用统一信号（见 lib/engine.py）

if __name__ == "__main__":
    engine.run_live_align(
        SYMBOL_KEY, alignment_signal, MA_PERIODS, VOLUME,
        kline_sec=KLINE_SEC, allow_trade=ALLOW_TRADE,
        web_gui=WEB_GUI, stop_pct=STOP_PCT,
        # 运行记录 + 账户流水单（2026-09-27 补齐）
        record_dir=RECORD_DIR,
        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,
        ledger_dir=LEDGER_DIR,
    )
