# -*- coding: utf-8 -*-
"""
通道突破策略（唐奇安 / 海龟原型）—— 模拟盘入口
==========================================================
跑之前先确认：
  - 回测已经跑过、逻辑你认可
  - 当前 ALLOW_TRADE = False（观察模式）：只打印信号、不向模拟账户发单；
    想真发单到快期模拟账户核对成交，再把它改成 True。
    ★ 2026-09-27 全项目 12 品种 × 5 策略统一为 False（此前 01 的 PVC 是唯一开着下单的）

怎么用：
  双击同目录的  模拟盘.bat
  程序要保持开着才会交易，收盘后 Ctrl+C 关掉即可。

★ 买卖逻辑与回测共用 lib/engine.py 的 channel_signal()，两侧不会走偏：
      空仓 + 收盘价 > 前 N1 根最高  -> 目标做多
      空仓 + 收盘价 < 前 N1 根最低  -> 目标做空
      持多 + 收盘价 < 前 N2 根最低  -> 目标空仓（平多离场，不反手）
      持空 + 收盘价 > 前 N2 根最高  -> 目标空仓（平空离场，不反手）
      其余                          -> 维持当前方向

★ 本策略是 5 个策略里唯一带 failsafe 过滤器的（海龟 System 1）：
      「上一笔突破交易若盈利，就跳过下一个突破信号」，只用一次即恢复。
      回测脚本一直开着它（turtle_failsafe=1）；为让两边信号不分叉，本模拟盘也开着。
      engine 的 run_live 于 2026-09-27 补上这条分支（此前 run_live 没有该参数，
      04 因此长期没有模拟盘）。
   ⚠️ 实时版的固有局限：这个状态只活在**进程内存**里 —— 重启程序就忘了
      「上一笔赚没赚」，会按「未盈利」从头开始。回测里全程连续，没有这个断点。
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
SYMBOL_KEY = "SR"
# ---- 海龟系统选择（与回测脚本同结构）----
#   System 1：入场 20 根 / 出场 10 根，**带 failsafe 过滤**（当前口径）
#   System 2：入场 55 根 / 出场 20 根，无过滤
#   ⚠️ 55 的口径：图里写「55 天」，本项目周期是 30 分钟，故取 55 根 30min K 线
#      （≈4 个交易日），与 20 根同一尺度 —— 用户 2026-09-20 裁定。
SYSTEM = "1"
_TURTLE = {
    "1": {"N1": 20, "N2": 10, "FAILSAFE": True},
    "2": {"N1": 55, "N2": 20, "FAILSAFE": False},
}[SYSTEM]
N1 = _TURTLE["N1"]         # 入场通道：收盘价突破「前 N1 根」最高/最低 -> 开仓
N2 = _TURTLE["N2"]         # 出场通道：收盘价反向突破「前 N2 根」另一侧 -> 平仓
FAILSAFE = _TURTLE["FAILSAFE"]   # 海龟 System 1 的 failsafe 过滤（System 2 = 关闭）
VOLUME = 1                 # 固定手数（仅当 TARGET_RISK_PCT = 0 时生效）
# ---- 仓位：波动率预算（与回测脚本同参数、同口径，12 品种统一）----
TARGET_RISK_PCT = 1.0      # 1.0 = 每笔目标风险 1%（20 万 → 2000 元）；0 = 关闭
HAND_MIN = 1               # 手数下限
HAND_MAX = 8               # 手数上限（低波动期 ATR 会低估后续风险，必须设硬顶）
RISK_EQUITY = 200000.0     # 算手数用的权益：与回测初始资金对齐，便于两边对账；
                           # 改成 0 则用账户实时权益 account.balance
KLINE_SEC = 30 * 60        # 30分钟（与回测脚本一致）
ALLOW_TRADE = False        # False = 只打印信号不下单（观察期，不真发单到模拟账户）
WEB_GUI = True             # 是否打开天勤 web_gui 面板（实时K线/信号/持仓/资金曲线）
# ---- 止损（与回测同口径：初始 ATR 止损 2×ATR，保本关闭）----
ATR_STOP_MULT = 2.0        # 初始止损：2×ATR(14)；0 = 关闭
BREAKEVEN_ATR = 0.0        # 保本触发：0 = 关闭
ATR_PERIOD = 14            # ATR 周期
# ---- 运行记录 ----
#   把「程序有没有在跑」和「有没有开仓条件」分开落盘，供事后做台账：
#     实测记录/04_通道突破/白糖/白糖_成交流水.csv  （事件流）
#     实测记录/04_通道突破/白糖/白糖_运行心跳.csv  （每 5 分钟一行，**休市照写**）
#   想关掉记录：把 RECORD_DIR 改成 None。
RECORD_DIR = os.path.join(_ROOT, "实测记录", "04_通道突破", "白糖")
RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）
# ⚠️ 这里**没有** NO_OPEN_WINDOW_MIN：04 的回测脚本本身就没用开盘保护
#    （其余四策略用了 30 分钟）。不传 => engine 默认 0 = 关闭，两侧口径一致。
# =================================================


if __name__ == "__main__":
    print(f"[海龟系统] System {SYSTEM} = 入场 {N1} 根 / 出场 {N2} 根 / "
          f"failsafe 过滤 {'开' if FAILSAFE else '关'} | 观察模式={not ALLOW_TRADE}")
    engine.run_live(
        SYMBOL_KEY, engine.channel_signal, N1, N2, VOLUME,
        kline_sec=KLINE_SEC,
        allow_trade=ALLOW_TRADE,
        web_gui=WEB_GUI,
        pass_dir=True,          # 目标仓位式：允许「只平仓、不反手」（出场即空仓等待）
        signal_kind="channel",  # ★ 信号值改从通道轨道来：突破开仓 / 反向破位平仓
        atr_stop_mult=ATR_STOP_MULT,
        breakeven_atr=BREAKEVEN_ATR,
        atr_period=ATR_PERIOD,
        risk_pct=TARGET_RISK_PCT,
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
        risk_equity=RISK_EQUITY,
        record_dir=RECORD_DIR,
        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,
        # ★ 海龟 System 1 的 failsafe 过滤（与回测脚本逐字同口径）。
        #   engine 里默认 0 = 不启用；01/02/03/05 都不传该参数，行为不受影响。
        turtle_failsafe=1 if FAILSAFE else 0,
    )
