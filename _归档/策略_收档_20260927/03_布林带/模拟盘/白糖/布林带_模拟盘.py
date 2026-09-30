# -*- coding: utf-8 -*-
# ⚠️ 本文件由「豆粕」版机械复制生成（2026-09-27 批量补品种，12 品种口径统一）。
#    品种相关处（SYMBOL_KEY / 报告路径 / 账本路径 / 滑点说明）已换成本品种；
#    docstring 里的版本沿革与实验数字记录的是**原品种「豆粕」**的过程，本品种未单独复现。
#    核心买卖逻辑在 lib/engine.py，各品种共用同一份，不会走偏。
"""
布林带策略 v3 + v4 —— 模拟盘入口（30分钟 · 均值回归）
==========================================================
逻辑与回测完全一致（同一套规则，见 lib/engine.boll_decision）。
本品种当前为观察模式（ALLOW_TRADE=False，未启用）；PVC 是在跑品种。

  · 上一根 30分钟 K 线收盘触及下轨 -> 开多；触及上轨 -> 开空（均值回归）
  · 出场：越过中轨另一侧 0.5×半带宽（超调出场，v1）
  · 时间止损：持仓满 20 根未出场 -> 假设失效离场（v2）
  · 止损：1×ATR(14) 固定线（锚=信号根收盘价，v3）
  · 手数（v4 2026-09-23）：波动率预算 0.30%，夹 [8, 25]
      hand = 基数 400,000 × 0.30% ÷ (1×ATR × 乘数) = 1200 元 / (1×ATR × 乘数)
      下限 8 = 合约最小限价开仓手数（消除静默拒单）
      上限 25 = 纯保护阀（历史最大手数 24.3，样本内零截断）
      基数 400,000 是「工作基数」：账户 100 万里锁死 60 万不动，
      risk_equity=400000 传给引擎 => 手数只在 40 万口径上算，与回测 init_balance 完全对齐
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
SYMBOL_KEY = "SR"
BOLL_PERIOD = 20                # 中轨均线周期
BOLL_K = 2.0                          # 上下轨 = 中轨 ± BOLL_K 倍标准差
VOLUME = 1                            # 基础手数（risk_pct=0 时才用它）
KLINE_SEC = 1800                  # K 线周期（30分钟）；v0 定稿
ATR_PERIOD = 14                 # ATR 周期（止损线与手数共用同一把尺子）
ATR_STOP_MULT = 1.0                  # v3（2026-09-22）：止损收窄一倍 = 1×ATR（锚=信号根收盘价，固定不移动）
RISK_PCT = 0.30                      # v4（2026-09-23）单笔风险 = 基数×0.30% = 1200 元
HAND_MIN, HAND_MAX = 8, 25           # v4：下限8=合约最小限价开仓手数；上限25=纯保护阀
TIME_STOP_BARS = 20   # v2（2026-09-22）：持仓满 20 根未出场 -> 假设失效离场（复用布林周期，定死不扫参）
ALLOW_TRADE = False                   # [!] 先观察：False=只打印信号不下单；确认无误再改 True
WEB_GUI = True                        # 是否打开天勤 web_gui 面板（实时K线+布林带+信号+持仓）
HEARTBEAT = 30                        # 盘面心跳间隔（秒）
RISK_EQUITY = 400000                  # v4：算手数用的固定基数（账户100万里锁死60万）
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
