# -*- coding: utf-8 -*-
"""
双均线策略 —— 模拟盘入口
==========================================================
跑之前先确认：
  - 回测已经跑过、逻辑你认可
  - 当前 ALLOW_TRADE = False（观察模式）：只打印信号、不向模拟账户发单。
    ★ 2026-09-27 按用户指示改的 —— 此前本品种是 12 个里唯一开着下单的，
      与其余 11 个口径不一致；统一退回观察模式，避免「双击就真发单」的意外。
  - 想看真实成交价与滑点（回测里的滑点是**假设**，等效 2 跳，只有真实成交能验）：
    把 ALLOW_TRADE 改成 True。改前想清楚 —— 交易时段一开就会真发单到模拟账户。

怎么用：
  双击同目录的  模拟盘.bat
  程序要保持开着才会交易，收盘后 Ctrl+C 关掉即可。
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
SYMBOL_KEY = "V"
N1 = 5                     # 快线
N2 = 20                    # 慢线
N3 = 60                    # 趋势过滤线 MA60：决定「能不能开反向仓」
VOLUME = 1                 # 固定手数（仅当 TARGET_RISK_PCT = 0 时生效）
KLINE_SEC = 30 * 60        # 30分钟（v5 2026-09-18：5分钟 → 30分钟；均线仍 5/20/60）
ALLOW_TRADE = False        # ★ 2026-09-27 按用户指示退回观察模式（与其余 11 个品种口径统一）：
                           #   只打印信号、不向模拟账户发单。想真发单就到快期模拟账户核对，
                           #   把上面这行改成 True —— 改前确认这是你想要的（交易时段一开就下单）。
LEDGER_DIR = os.path.join(_ROOT, "实测记录", "账户")   # 账户流水单目录（2026-09-24 新增）
    # ★ 账户视角：不管哪个策略/哪个品种，账户里所有真实成交都记进这里
    #   · 成交明细.csv —— 每笔成交：成交价/委托价/滑点/手续费/平仓盈亏/成交额/拒单
    #   · 账户快照.csv —— 每 5 分钟一次账户全景：权益/可用/保证金/浮盈/持仓明细
    #   · 流水单事件.log —— 漏单、方向冲突等
    #   与「策略视角」的 _成交流水.csv 互补，互不替代。
                           #   目的是实测成交价与滑点（回测滑点是假设，只有真实成交能验）。
                           #   False = 只打印信号不下单（观察模式）。其余 4 个品种未改，仍是 False。
WEB_GUI = True             # 是否打开天勤 web_gui 面板（实时K线/信号/持仓/资金曲线）
# ---- 止损（2026-09-18 v4 · 回滚到阶段A：初始 ATR 止损 2×ATR，保本关闭）----
# 与回测脚本同参数、同口径（engine 里 run_backtest / run_live 用的是同一份判断）。
# 阶段A：开仓那一刻锁定 entry_atr，止损线 = 开仓价 ∓ 2×ATR(14)，触发即平仓。
# 阶段B 的保本本次关闭（=0）：它把「小亏」变「零亏」，但净额打平略负。
ATR_STOP_MULT = 2.0        # 初始止损：2×ATR(14)；0 = 关闭
BREAKEVEN_ATR = 0.0        # 保本触发：0 = 关闭（v4 回滚到阶段A）
ATR_PERIOD = 14            # ATR 周期
# ---- 仓位：v6 波动率预算（2026-09-19 · 与回测脚本同参数、同口径）----
#   手数 = 权益 × TARGET_RISK_PCT ÷ (ATR_STOP_MULT × 信号根ATR × 合约乘数)，
#   夹在 [HAND_MIN, HAND_MAX]，开仓那一根锁定、持仓期不变（不加仓、不递减）。
#   与止损线共用同一个 ATR：止损挂在 开仓价 ∓ 2×ATR 处，所以被止损打掉时
#   亏的钱正好 ≈ 目标风险额 —— 风险预算与实际风险是同一把尺子。
TARGET_RISK_PCT = 1.0      # 1.0 = 每笔目标风险 1%（20 万 → 2000 元）；0 = 关闭，退回 VOLUME 手
HAND_MIN = 1               # 手数下限
HAND_MAX = 8               # 手数上限（低波动期 ATR 会低估后续风险，必须设硬顶）
RISK_EQUITY = 200000.0     # 算手数用的权益：与回测初始资金对齐，便于两边对账；
                           # 改成 0 则用账户实时权益 account.balance
# ---- 运行记录（2026-09-20 · v7）----
#   把「程序有没有在跑」和「有没有开仓条件」分开落盘，供事后做台账：
#     实测记录/01_双均线/PVC/PVC_成交流水.csv  （事件流）
#     实测记录/01_双均线/PVC/PVC_运行心跳.csv  （每 5 分钟一行，**休市照写**）
#   心跳休市照写 -> 台账里凡出现「时间断层」就只能是程序当时没在跑；
#   心跳连续却没有 SIGNAL 行 = 程序在跑、条件不够。两者因此能区分开。
#   想关掉记录：把 RECORD_DIR 改成 None。
RECORD_DIR = os.path.join(_ROOT, "实测记录", "01_双均线", "PVC")
RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）

# ---- 开盘保护（2026-09-21 · v7 同步，与回测同口径）----
#   日盘 09:00 / 夜盘 21:00 开盘后前 NO_OPEN_WINDOW_MIN 分钟内不开新仓；
#   平仓与反手不受限；被拦的信号丢弃（不是推迟）。0 = 关闭。
NO_OPEN_WINDOW_MIN = 30

# =================================================


# 买卖逻辑在 lib/engine.py 的 trend_reverse_signal()，与回测共用同一份，两侧不会走偏。
#   金叉 且 MA20 >  MA60 -> 做多     ｜  金叉 且 MA20 <= MA60 -> 只平空、空仓等待
#   死叉 且 MA20 <  MA60 -> 做空     ｜  死叉 且 MA20 >= MA60 -> 只平多、空仓等待
#   另外 v4 起用「初始 ATR 止损」：开仓价 ∓ 2×ATR(14)，触及即平仓（保本已关闭）。
#   开仓手数「波动率预算」（见上方参数）与回测同口径，五品种模拟盘脚本参数完全一致。
if __name__ == "__main__":
    engine.run_live(
        SYMBOL_KEY, engine.trend_reverse_signal, N1, N2, VOLUME,
        kline_sec=KLINE_SEC,
        allow_trade=ALLOW_TRADE,
        web_gui=WEB_GUI,
        ma_filter=N3,      # MA60 趋势过滤
        pass_dir=True,     # 目标仓位式：允许「只平仓、不反手」
        atr_stop_mult=ATR_STOP_MULT,   # v4(2026-09-18)：初始 ATR 止损 2×ATR（阶段A）
        breakeven_atr=BREAKEVEN_ATR,   # v4(2026-09-18)：保本关闭 = 0
        atr_period=ATR_PERIOD,
        # v6(2026-09-19)：波动率预算手数 —— 与回测脚本同一份口径，本次唯一改动点。
        # TARGET_RISK_PCT=0 时完全不生效，退回 VOLUME 手。
        risk_pct=TARGET_RISK_PCT,
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
        risk_equity=RISK_EQUITY,
        # v7(2026-09-20)：运行记录落盘（成交流水 + 运行心跳）。
        # 只落盘、不参与任何交易判断；record_dir=None 时完全不记录。
        record_dir=RECORD_DIR,
        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,
        # v7 同步(2026-09-21)：开盘保护 —— 与回测 no_open_window_min=30 同一份口径。
        no_open_window_min=NO_OPEN_WINDOW_MIN,
        heartbeat=300,   # 2026-09-21：盘面心跳每 5 分钟打印一行（肉眼可监控中断；CSV 那层仍休市照写，不动）
        ledger_dir=LEDGER_DIR,
    )
