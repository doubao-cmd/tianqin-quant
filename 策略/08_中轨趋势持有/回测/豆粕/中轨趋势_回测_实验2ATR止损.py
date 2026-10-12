# -*- coding: utf-8 -*-
"""中轨趋势持有 · 实验：加 2×ATR 固定止损（单变量）
==========================================================
★ 本文件是「08 裸版」的**单变量实验版**，不是新卡片、不是新策略。
   与裸版 `中轨趋势_回测.py` 的**唯一差异**：ATR_STOP_MULT 0.0 -> 2.0。
   （RISK_PCT 同步置 0 = 保持固定 1 手，否则 2ATR 会连带把「固定 1 手」
     变成「波动率预算 2~8 手」= 一处参数两处改动，归因不干净。）

实验目的：回答「给一个信号层 ≈ 无 edge 的策略加止损，能不能救回来」。
先验（写在跑之前，防事后找理由）：**大概率不能**。理由：
  · 止损只改变亏损的**分布形状**，不产生 edge；
  · 中轨趋势是「永远在场」的翻转策略，被止损打掉后下一根仍可能反手 ->
    换手增加 -> 成本增加（本策略成本已是主要亏损来源）。
预期：亏损未必更小，且**笔数会增加**。若真变好，需警惕是不是运气。

对照基线（裸版，同一品种同一区间）：
  豆粕 M：收益 −1.80% ｜ 回撤 9.49% ｜ 夏普 −0.8221 ｜ 平仓 174 笔 ｜ 成交 349 笔

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
ATR_STOP_MULT = 2.0             # ★★ 唯一的变量：裸版 0.0 -> 实验 2.0（固定止损）
RISK_PCT = 0.0                  # ★ 置 0 = 手数固定 1 手（隔离「止损」与「手数」两个变量）
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
STRATEGY_NAME = "中轨趋势持有_实验2ATR止损"   # 报告名带实验后缀，不覆盖裸版产物
# =================================================


if __name__ == "__main__":
    print(f"[实验] 2×ATR 固定止损 | 品种 {SYMBOL_KEY} | 区间 {START} ~ {END}")
    print(f"[实验] 单变量声明：ATR_STOP_MULT 0.0 -> {ATR_STOP_MULT}（其余与裸版逐字相同）")
    print(f"[实验] RISK_PCT={RISK_PCT} ⇒ 手数固定 {VOLUME} 手（与裸版一致，隔离变量）")
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
        atr_stop_mult=ATR_STOP_MULT,     # ★ 唯一变量
        risk_pct=RISK_PCT,               # 0 = 固定手数
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
    )
