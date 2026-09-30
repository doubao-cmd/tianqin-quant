# -*- coding: utf-8 -*-
"""
均值回归策略 —— 模拟盘入口
==========================================================
跑之前先确认：
  - 回测已经跑过、逻辑你认可
  - 当前 ALLOW_TRADE = False（观察模式）：只打印信号、不向模拟账户发单；
    想真发单到快期模拟账户核对成交，再把它改成 True

怎么用：
  双击同目录的  模拟盘.bat
  程序要保持开着才会交易，收盘后 Ctrl+C 关掉即可。

★ 买卖逻辑与回测共用 lib/engine.py 的 rev_signal()，两侧不会走偏：
      收盘价 < MA(N1)  -> 目标做多
      收盘价 > MA(N1)  -> 目标做空
      穿越均线即反手（永远在场）
"""
import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))  # 从脚本所在目录开始
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)  # 逐级向上，找到含 lib 的项目根目录为止
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ==================== 可调参数 ====================
SYMBOL_KEY = "EB"
N1 = 20                    # 均值周期 MA(20) —— 与回测脚本同值
N2 = 20                    # signal_kind="rev" 用不到，占位
VOLUME = 1                 # 固定手数（仅当 TARGET_RISK_PCT = 0 时生效）
# ---- 仓位：波动率预算（与回测脚本同参数、同口径，12 品种统一）----
TARGET_RISK_PCT = 1.0      # 1.0 = 每笔目标风险 1%（20 万 → 2000 元）；0 = 关闭
HAND_MIN = 1               # 手数下限
HAND_MAX = 8               # 手数上限
RISK_EQUITY = 200000.0     # 算手数用的权益：与回测初始资金对齐，便于两边对账；
                           # 改成 0 则用账户实时权益 account.balance
KLINE_SEC = 30 * 60        # 30分钟
ALLOW_TRADE = False        # False = 只打印信号不下单（观察期，不真发单到模拟账户）
WEB_GUI = True             # 是否打开天勤 web_gui 面板（实时K线/信号/持仓/资金曲线）
# ---- 止损（与回测同口径：初始 ATR 止损 2×ATR，保本关闭）----
ATR_STOP_MULT = 2.0        # 初始止损：2×ATR(14)；0 = 关闭
BREAKEVEN_ATR = 0.0        # 保本触发：0 = 关闭
ATR_PERIOD = 14            # ATR 周期
# ---- 运行记录 ----
#   把「程序有没有在跑」和「有没有开仓条件」分开落盘，供事后做台账：
#     实测记录/03_均值回归/苯乙烯/苯乙烯_成交流水.csv  （事件流）
#     实测记录/03_均值回归/苯乙烯/苯乙烯_运行心跳.csv  （每 5 分钟一行，**休市照写**）
#   想关掉记录：把 RECORD_DIR 改成 None。
RECORD_DIR = os.path.join(_ROOT, "实测记录", "03_均值回归", "苯乙烯")
RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）
# ---- 开盘保护（与回测同口径）----
NO_OPEN_WINDOW_MIN = 30
# =================================================


if __name__ == "__main__":
    engine.run_live(
        SYMBOL_KEY, engine.rev_signal, N1, N2, VOLUME,
        kline_sec=KLINE_SEC,
        allow_trade=ALLOW_TRADE,
        web_gui=WEB_GUI,
        pass_dir=True,       # 目标仓位式
        signal_kind="rev",   # ★ 与回测同一份信号：单均线穿越即反手
        atr_stop_mult=ATR_STOP_MULT,
        breakeven_atr=BREAKEVEN_ATR,
        atr_period=ATR_PERIOD,
        risk_pct=TARGET_RISK_PCT,
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
        risk_equity=RISK_EQUITY,
        record_dir=RECORD_DIR,
        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,
        no_open_window_min=NO_OPEN_WINDOW_MIN,
    )
