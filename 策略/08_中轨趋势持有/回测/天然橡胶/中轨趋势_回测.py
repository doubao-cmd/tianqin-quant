# -*- coding: utf-8 -*-
"""中轨趋势持有 —— 回测入口（日线 · 裸版）
==========================================================
策略逻辑（核心共用 lib/engine.trend_hold_signal，回测与模拟盘同一套规则）：
  · 周期：日线 K 线（86400 秒）
  · 中轨 = MA20（收盘价简单均值）
  · 信号（永远在场）：收盘 > 中轨 -> 目标 +1（持多）；收盘 < 中轨 -> 目标 -1（持空）
  · 出场 = 下次方向翻转（本策略没有「空仓等待」，多空镜像）
  · 止损：无（裸版；2×ATR 与时间止损留第二轮，各自单变量）
  · 手数：波动率预算 1%（= 权益×1% ÷ (2×ATR×乘数)，夹 [1,8]）
  · 零过滤：无趋势闸门、无时段过滤（先测「MA20 穿越本身有无 edge」）
  · 信号只用已收盘 K 线，下单在下一根 K 线开盘成交（无未来函数）

★ 命名澄清：本策略只用布林带的「中轨」，上/下轨与标准差全程不参与
  ⇒ 等价于「收盘价 vs MA20 的单均线穿越」，不是完整布林带策略。

★ 与 03 均值回归(rev_signal)的关系：本策略是它的**顺势镜像版**，
  换手次数完全相同（都是均线穿越次数），只是持仓方向对调。

怎么用：
  1. 改下面「可调参数」
  2. 双击同目录 回测.bat
  3. 浏览器自动出报告（资金曲线 + 逐笔成交 + 指标卡片）

注：MA20 需约 20 根 K 线完成预热，START 已前移到评估区间前 1 个月；
EVAL_START 之前只预热、不交易、不标注买卖点。
样本内(练习)/样本外(期末考试) 切分见下方「可调参数」里的 SAMPLE / IS_* / OOS_*。
"""
import sys
import os
from datetime import date, timedelta

# 把项目根目录（天勤量化/）加到搜索路径，import lib / config 才找得到
_ROOT = os.path.dirname(os.path.abspath(__file__))  # 从脚本所在目录开始
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)  # 逐级向上，找到含 lib 的项目根目录为止
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ==================== 可调参数 ====================
# 本文件夹对应的品种（已写死在下方 SYMBOL_KEY）
SYMBOL_KEY = "RU"
MA_PERIOD = 20                  # 中轨均线周期（唯一参数，用户指定，不扫描）
VOLUME = 1                      # 基础手数（risk_pct=0 时才用它）
KLINE_SEC = 86400               # K 线周期 = 日线
ATR_PERIOD = 14                 # ATR 周期（仅用于手数的风险刻度，本卡无止损）
ATR_STOP_MULT = 0.0             # ★ 裸版：无价格止损
RISK_PCT = 1.0                  # 波动率预算：单笔风险 = 权益×1%（同步 MA13/07 口径）
HAND_MIN, HAND_MAX = 1, 8       # 手数上下限
TIME_STOP_BARS = None           # ★ 裸版：无时间止损

# ---- 样本内(练习) / 样本外(期末考试) 切分（规则3：样本外仅用于最终验证，看过一次即作废）----
# 练习(样本内)     = 2018-01-01 ~ 2023-12-31   用于开发/判定
# 期末考试(样本外) = 2024-01-01 ~ 2026-09-26   前向验证专用，从没看过，只跑一次
#   沿用项目「大周期样本划分」口径（与出水芙蓉卡等一致）
IS_START  = date(2018, 1, 1)    # 练习(样本内)开始
IS_END    = date(2023, 12, 31)  # 练习(样本内)结束
OOS_START = date(2024, 1, 1)    # 期末考试(样本外)开始
OOS_END   = date(2026, 9, 26)   # 期末考试(样本外)结束
SAMPLE = "in"                   # "in"=练习(可反复跑) | "out"=期末考试(只跑一次)
assert SAMPLE in ("in", "out"), f"SAMPLE 只能填 'in' 或 'out'，现在是 {SAMPLE!r}"
EVAL_START = IS_START if SAMPLE == "in" else OOS_START
EVAL_END   = IS_END   if SAMPLE == "in" else OOS_END
START = EVAL_START - timedelta(days=40)   # 预热：MA20(日线) 约需 20 个交易日，前移 40 天足够
END   = EVAL_END
INIT_BALANCE = 200000                # 初始资金（元）
WEB_GUI = False                       # 跑完用浏览器打开报告
# =================================================


if __name__ == "__main__":
    _label = {"in": "练习(样本内，可反复跑)", "out": "期末考试(样本外，只跑一次)"}[SAMPLE]
    print(f"[样本模式] {_label} | 回测区间 {START} ~ {END}")
    if SAMPLE == "out":
        print("!! 期末考试提示：样本外区间只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
    engine.run_backtest(
        SYMBOL_KEY, engine.trend_hold_signal,
        n1=MA_PERIOD, n2=MA_PERIOD, volume=VOLUME,
        start=START, end=END, eval_start=EVAL_START,
        init_balance=INIT_BALANCE, kline_sec=KLINE_SEC,
        strategy_name="中轨趋势持有", open_html=WEB_GUI, sample_tag=SAMPLE,
        slip_ticks=1,               # 1 跳滑点（与 01/07 同口径，折进 TqSim 手续费）
        pass_dir=True,              # ★ 目标仓位式（trendhold 必须）
        signal_kind="trendhold",    # ★ 08 中轨趋势持有信号路径
        atr_period=ATR_PERIOD,
        atr_stop_mult=ATR_STOP_MULT,
        risk_pct=RISK_PCT,
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
        # time_stop_bars 裸版不传（engine 默认 None）
    )
