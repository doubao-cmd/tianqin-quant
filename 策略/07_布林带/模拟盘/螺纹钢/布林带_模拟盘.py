# -*- coding: utf-8 -*-
"""
布林带策略 v3 + 统一口径 —— 模拟盘入口（30分钟 · 均值回归）
==========================================================
逻辑与回测完全一致（同一套规则，见 lib/engine.boll_decision）。
本品种当前为观察模式（ALLOW_TRADE=False，未启用）；PVC 是在跑品种。

  · 上一根 30分钟 K 线收盘触及下轨 -> 开多；触及上轨 -> 开空（均值回归）
  · 出场：越过中轨另一侧 0.5×半带宽（超调出场，v1）
  · 时间止损：持仓满 20 根未出场 -> 假设失效离场（v2）
  · 止损：1×ATR(14) 固定线（锚=信号根收盘价，v3）
  · 手数（2026-10-13 统一口径）：波动率预算 1%，夹 [1, 8]（与 06/08 同口径）
      hand = 基数 200,000 × 1% ÷ (1×ATR × 乘数) = 2000 元 / (1×ATR × 乘数)
      基数 200,000 与回测 INIT_BALANCE 严格对齐（06 亦锁死 20 万）
      ⚠️ 已知代价：1×ATR 下 4 个品种手数多数撞上限 8 ⇒ 退化为「固定 8 手」
         （详见同品种回测入口 docstring 的说明）
  · 同一时间只允许一个方向持仓，不锁仓

提示：跑本脚本会登录你的快期模拟账户，此时不要再把快期3 登在同一账户上（会互相顶掉）。
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
SYMBOL_KEY = "RB"
BOLL_PERIOD = 20                # 中轨均线周期
BOLL_K = 2.0                          # 上下轨 = 中轨 ± BOLL_K 倍标准差
VOLUME = 1                            # 基础手数（risk_pct=0 时才用它）
KLINE_SEC = 1800                  # K 线周期（30分钟）；v0 定稿
ATR_PERIOD = 14                 # ATR 周期（止损线与手数共用同一把尺子）
ATR_STOP_MULT = 1.0                  # v3（2026-09-22）：止损收窄一倍 = 1×ATR（锚=信号根收盘价，固定不移动）
RISK_PCT = 1.0                       # 2026-10-13 统一口径：单笔风险 = 基数×1% = 2000 元（与 06/08 同）
HAND_MIN, HAND_MAX = 1, 8            # 2026-10-13 统一口径：与 06/08 同（⚠️ 1×ATR 下上限 8 常 binding）
TIME_STOP_BARS = 20   # v2（2026-09-22）：持仓满 20 根未出场 -> 假设失效离场（复用布林周期，定死不扫参）
ALLOW_TRADE = False                   # [!] 先观察：False=只打印信号不下单；确认无误再改 True
WEB_GUI = True                        # 是否打开天勤 web_gui 面板（实时K线+布林带+信号+持仓）
HEARTBEAT = 30                        # 盘面心跳间隔（秒）
RISK_EQUITY = 200000                  # 2026-10-13 统一口径：算手数基数锁死 20 万（与 06 同）
# =================================================


if __name__ == "__main__":
    engine.run_live_boll(
        SYMBOL_KEY, period=BOLL_PERIOD, k=BOLL_K, volume=VOLUME,
        kline_sec=KLINE_SEC, allow_trade=ALLOW_TRADE,
        web_gui=WEB_GUI, heartbeat=HEARTBEAT,
        atr_period=ATR_PERIOD, atr_stop_mult=ATR_STOP_MULT,
        risk_pct=RISK_PCT, risk_hand_min=HAND_MIN, risk_hand_max=HAND_MAX,
        risk_equity=RISK_EQUITY,
        time_stop_bars=TIME_STOP_BARS,
    )
