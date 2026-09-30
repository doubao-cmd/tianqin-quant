# -*- coding: utf-8 -*-
"""
均线多空排列策略 —— 模拟盘入口（30分钟 · 双向排列 + MACD 交叉，PVC 专用版本）
==========================================================
逻辑与回测完全一致（看 alignment_signal）。先观察、再真下单。
双向：多头排列+水上金叉开多 / 空头排列+水下死叉开空（完全镜像）。
平多=MACD 死叉 / 平空=MACD 金叉（完全镜像）。
止损=开仓时定死 2.0×ATR 固定线（多空对称，不移动）。
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
MA_PERIODS = (5, 10, 20, 40)          # 四根均线，判断多/空排列（短->长）
VOLUME = 1
KLINE_SEC = 30 * 60                    # 30 分钟 K 线（与回测同口径）
ALLOW_TRADE = False                   # [!] 先观察：False = 只打印信号不下单；确认无误改 True
LEDGER_DIR = os.path.join(_ROOT, "实测记录", "账户")   # 账户流水单目录（2026-09-24 新增）
    # ★ 账户视角：不管哪个策略/哪个品种，账户里所有真实成交都记进这里
    #   · 成交明细.csv —— 每笔成交：成交价/委托价/滑点/手续费/平仓盈亏/成交额/拒单
    #   · 账户快照.csv —— 每 5 分钟一次账户全景：权益/可用/保证金/浮盈/持仓明细
    #   · 流水单事件.log —— 漏单、方向冲突等
    #   与「策略视角」的 _成交流水.csv 互补，互不替代。
WEB_GUI = True             # 是否打开天勤 web_gui 面板（实时K线/信号/持仓/资金曲线）
STOP_PCT = 0.0                        # 百分比止损（0=关闭）

# ---- 运行记录（2026-09-27 补齐，与 01/03/04/05 一致）----
#   把「程序有没有在跑」和「有没有开仓条件」分开落盘，供事后做台账：
#     实测记录/02_均线排列/PVC/PVC_成交流水.csv  （事件流）
#     实测记录/02_均线排列/PVC/PVC_运行心跳.csv  （每 5 分钟一行，**休市照写**）
#   想关掉记录：把 RECORD_DIR 改成 None。
RECORD_DIR = os.path.join(_ROOT, "实测记录", "02_均线排列", "PVC")
RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）
STOP_ATR = 2.0                        # 固定 ATR 止损倍数（开仓时定死不移动；0=关闭）
# =================================================


alignment_signal = engine.align_signal_dual_macd  # 双向统一信号（空头侧完全镜像，见 lib/engine.py）

if __name__ == "__main__":
    engine.run_live_align(
        SYMBOL_KEY, alignment_signal, MA_PERIODS, VOLUME,
        kline_sec=KLINE_SEC, allow_trade=ALLOW_TRADE,
        web_gui=WEB_GUI, stop_pct=STOP_PCT,
        stop_atr=STOP_ATR,
        ledger_dir=LEDGER_DIR,
        record_dir=RECORD_DIR,
        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,
    )
