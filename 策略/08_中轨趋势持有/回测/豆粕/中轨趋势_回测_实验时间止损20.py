# -*- coding: utf-8 -*-
"""中轨趋势持有 · 实验：加时间止损 20 根（单变量）
==========================================================
★ 本文件是「08 裸版」的**单变量实验版**。
   与裸版 `中轨趋势_回测.py` 的**唯一差异**：新增 `time_stop_bars=20`。
   （RISK_PCT=0 保持固定 1 手，与 2ATR 实验、裸版逐项可比。）

实验目的：回答「给一个信号层 ≈ 无 edge 的策略加时间止损，能不能救回来」。
先验（写在跑之前，防事后找理由）：**大概率不能，甚至更差**。理由：
  · 时间止损同样只改变出场时点，不产生 edge；
  · 本策略是「永远在场」的翻转结构，时间止损把你「等 MA 反转」换成「到期即平」
    —— 而到期点未必是合理出场点；且平掉后下一根往往又按同方向进场
    ⇒ 换手不减少、只多付一次成本。
  · 第二轮（2×ATR）实测已显示同一机制使结果**恶化**（收益 −1.80%→−3.16%）。

⚠️ 本轮需要给 engine 的 run_backtest 新增 time_stop_bars 能力
   （此前只有 run_backtest_boll 有；口径与其逐字一致，已加，默认关闭、向后兼容）。

对照基线（同品种同区间）：
  裸版     ：收益 −1.80% ｜ 夏普 −0.8221 ｜ 回撤 9.49% ｜ 平仓 174 ｜ 成交 349
  2ATR实验 ：收益 −3.16% ｜ 夏普 −0.89   ｜ 回撤 9.77% ｜ 平仓 175 ｜ 成交 351

怎么用：直接 python 跑本文件（约 26 分钟）。
"""
import sys
import os
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ==================== 参数（与裸版逐项相同，只有标注处不同）====================
SYMBOL_KEY = "M"
MA_PERIOD = 20
VOLUME = 1
KLINE_SEC = 86400
ATR_PERIOD = 14
ATR_STOP_MULT = 0.0             # 无价格止损（与裸版、与 2ATR 实验同基准）
TIME_STOP_BARS = 20             # ★★ 唯一的变量：持仓满 20 根已收盘 K 线未出场 -> 离场
RISK_PCT = 0.0                  # ★ 固定 1 手（隔离「止损」与「手数」两个变量）
HAND_MIN, HAND_MAX = 1, 8

IS_START  = date(2018, 1, 1)
IS_END    = date(2023, 12, 31)
OOS_START = date(2024, 1, 1)
OOS_END   = date(2026, 9, 26)
SAMPLE = "in"
assert SAMPLE in ("in", "out")
EVAL_START = IS_START if SAMPLE == "in" else OOS_START
EVAL_END   = IS_END   if SAMPLE == "in" else OOS_END
START = EVAL_START - timedelta(days=40)
END   = EVAL_END
INIT_BALANCE = 200000
WEB_GUI = False
STRATEGY_NAME = "中轨趋势持有_实验时间止损20"
# =================================================


if __name__ == "__main__":
    print(f"[实验] 时间止损 {TIME_STOP_BARS} 根 | 品种 {SYMBOL_KEY} | 区间 {START} ~ {END}")
    print(f"[实验] 单变量声明：新增 time_stop_bars={TIME_STOP_BARS}（其余与裸版逐字相同）")
    print(f"[实验] RISK_PCT={RISK_PCT} ⇒ 手数固定 {VOLUME} 手；ATR_STOP_MULT={ATR_STOP_MULT} ⇒ 无价格止损")
    engine.run_backtest(
        SYMBOL_KEY, engine.trend_hold_signal,
        n1=MA_PERIOD, n2=MA_PERIOD, volume=VOLUME,
        start=START, end=END, eval_start=EVAL_START,
        init_balance=INIT_BALANCE, kline_sec=KLINE_SEC,
        strategy_name=STRATEGY_NAME, open_html=WEB_GUI, sample_tag=SAMPLE,
        slip_ticks=1,
        pass_dir=True,
        signal_kind="trendhold",
        atr_period=ATR_PERIOD,
        atr_stop_mult=ATR_STOP_MULT,
        time_stop_bars=TIME_STOP_BARS,   # ★ 唯一变量（engine 2026-10-12 新增能力）
        risk_pct=RISK_PCT,
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
    )
