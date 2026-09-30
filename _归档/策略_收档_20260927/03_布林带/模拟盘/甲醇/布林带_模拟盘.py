# -*- coding: utf-8 -*-
"""

⚠️ 本品种已出局（2026-09-23 用户拍板）——参数维持旧口径，仅作历史留档，不要照此下单。
   理由：甲醇单手风险最大（1×ATR×10，均值 261 元）。要让手数 ≥ 8 手需单笔预算 ≥ 2088 元，
   而螺纹钢要 ≤16 手需预算 ≤ 1225 元 —— 五品种统一预算的交集为空。
   甲醇样本内横截面 −29.42%（五品种第二差），故不纳入 v4 统一手数方案。
   保留品种：PVC / 纯碱 / 螺纹钢 / 豆粕。布林带策略 v0 —— 模拟盘入口（30分钟 · 均值回归裸版）
==========================================================
逻辑与回测完全一致（同一套规则，见 lib/engine.boll_decision）。先观察、再真下单。

  · 上一根 30分钟 K 线收盘触及下轨 -> 开多；触及上轨 -> 开空（v0 均值回归）
  · 价格回到中轨 -> 平仓
  · 止损：2×ATR(14) 固定线（锚=信号根收盘价，与 01 双均线同口径）
  · 手数：波动率预算 1%，夹 [1,8]
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
SYMBOL_KEY = "MA"
BOLL_PERIOD = 20                # 中轨均线周期
BOLL_K = 2.0                          # 上下轨 = 中轨 ± BOLL_K 倍标准差
VOLUME = 1                            # 基础手数（risk_pct=0 时才用它）
KLINE_SEC = 1800                  # K 线周期（30分钟）；v0 定稿
ATR_PERIOD = 14                 # ATR 周期（止损线与手数共用同一把尺子）
ATR_STOP_MULT = 1.0                  # v3（2026-09-22）：止损收窄一倍 = 1×ATR（锚=信号根收盘价，固定不移动）
RISK_PCT = 1.0                       # 波动率预算：单笔风险 = 权益×1%
HAND_MIN, HAND_MAX = 1, 8            # 手数上下限
TIME_STOP_BARS = 20   # v2（2026-09-22）：持仓满 20 根未出场 -> 假设失效离场（复用布林周期，定死不扫参）
ALLOW_TRADE = False                   # [!] 先观察：False=只打印信号不下单；确认无误再改 True
WEB_GUI = True                        # 是否打开天勤 web_gui 面板（实时K线+布林带+信号+持仓）
HEARTBEAT = 30                        # 盘面心跳间隔（秒）
# =================================================


if __name__ == "__main__":
    engine.run_live_boll(
        SYMBOL_KEY, period=BOLL_PERIOD, k=BOLL_K, volume=VOLUME,
        kline_sec=KLINE_SEC, allow_trade=ALLOW_TRADE,
        web_gui=WEB_GUI, heartbeat=HEARTBEAT,
        atr_period=ATR_PERIOD, atr_stop_mult=ATR_STOP_MULT,
        risk_pct=RISK_PCT, risk_hand_min=HAND_MIN, risk_hand_max=HAND_MAX,
        time_stop_bars=TIME_STOP_BARS,
    )
