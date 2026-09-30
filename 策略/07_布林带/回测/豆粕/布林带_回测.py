# -*- coding: utf-8 -*-
"""
布林带策略 v3 + v4 —— 回测入口（30分钟 · 均值回归）
==========================================================
策略逻辑（核心共用 lib/engine.boll_decision，回测与模拟盘同一套规则）：
  · 周期：30分钟 K 线
  · 布林带：中轨 = MA20，上/下轨 = 中轨 ± 2.0 倍标准差（标准参数，不扫描）
  · 开仓（空仓时）：上一根已收盘 K 线 收盘触及下轨 -> 开多；触及上轨 -> 开空
  · 出场（v3 2026-09-22）：超调出场（中轨 + 0.5×半带宽）/ 时间止损 20 根 / 1×ATR 止损
  · 止损：1×ATR(14) 固定线（锚=信号根收盘价，开仓定死、不移动）
  · 手数（v4 2026-09-23）：波动率预算 0.30%，只夹下限 8、上限 25 只作保护
      hand = 基数 400,000 × 0.30% ÷ (1×ATR × 乘数)，夹 [8, 25]
      下限 8 = 合约最小限价开仓手数（消除静默拒单）
      8~25 浮动 = 手数随 ATR 反向浮动 => 单笔风险归一到 1,200 元/笔
      （旧口径 HAND_MAX=8 把预算截断成固定 8 手，风险随 ATR 漂移 2.5~4.1 倍，
        波动率风控等于没有。v4 后风险离散降到 1.0~1.75 倍）
      上限 25 = 纯保护阀：历史最大手数 24.3 手，25 在样本内零截断，
        只在 ATR 异常收缩时兜底（防止手数失控）
  · 零过滤：无趋势闸门、无时段过滤（v0 起的定稿，加过滤 = 过拟合）
  · 信号只用已收盘 K 线，下单在下一根 K 线开盘成交（无未来函数）

怎么用：
  1. 改下面「可调参数」
  2. 双击同目录 回测.bat
  3. 浏览器自动出报告（资金曲线 + 逐笔成交 + 指标卡片）

注：MA20 需约 20 根 K 线完成预热，START 已前移到评估区间前 7 天；
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
SYMBOL_KEY = "M"
BOLL_PERIOD = 20                # 中轨均线周期
BOLL_K = 2.0                          # 上下轨 = 中轨 ± BOLL_K 倍标准差
VOLUME = 1                            # 基础手数（risk_pct=0 时才用它）
KLINE_SEC = 1800                  # K 线周期（30分钟）；v0 定稿
ATR_PERIOD = 14                 # ATR 周期（止损线与手数共用同一把尺子）
ATR_STOP_MULT = 1.0                  # v3（2026-09-22）：止损收窄一倍 = 1×ATR（锚=信号根收盘价，固定不移动）
RISK_PCT = 1.0                      # v4（2026-09-23）波动率预算：单笔风险 = 基数×0.30% = 1200 元
HAND_MIN, HAND_MAX = 1, 8           # v4：下限8=合约最小限价开仓手数；上限25=纯保护阀(历史最大24.3)
TIME_STOP_BARS = 20   # v2（2026-09-22）：持仓满 20 根未出场 -> 假设失效离场（复用布林周期，定死不扫参）
# ---- 样本内(练习) / 样本外(期末考试) 切分（规则3：样本外仅用于最终验证，看过一次即作废）----
# 练习(样本内)     = 2021-01-01 ~ 2023-12-31   一直用来开发/调参的那段，可反复跑
# 期末考试(样本外) = 2024-01-01 ~ 2025-12-31   前向验证专用，从没看过，只跑一次
#   ⚠ 旧切分 2026-03~09(练习)/2025-09~02(期末考) 已弃用；2026-09-29 用户拍板整体迁到 21-23 练习 + 24-25 期末考。
# 切分由用户 2026-09-29 决定（"样本区间改成 21-23 年"），定稿后不得再改。
IS_START  = date(2021, 1, 1)   # 练习(样本内)开始
IS_END    = date(2023, 12, 31)  # 练习(样本内)结束
OOS_START = date(2024, 1, 1)   # 期末考试(样本外)开始
OOS_END   = date(2025, 12, 31) # 期末考试(样本外)结束
SAMPLE = "in"                   # "in"=练习(可反复跑) | "out"=期末考试(只跑一次)
assert SAMPLE in ("in", "out"), f"SAMPLE 只能填 'in' 或 'out'，现在是 {SAMPLE!r}"
EVAL_START = IS_START if SAMPLE == "in" else OOS_START
EVAL_END   = IS_END   if SAMPLE == "in" else OOS_END
START = EVAL_START - timedelta(days=7)   # 预热：MA20 约需 20 根，前移 1 周足够
END   = EVAL_END
INIT_BALANCE = 200000                # v4：40万工作基数（模拟盘账户100万里锁死60万）
WEB_GUI = True                       # 跑完用浏览器打开报告
# =================================================


if __name__ == "__main__":
    _label = {"in": "练习(样本内，可反复跑)", "out": "期末考试(样本外，只跑一次)"}[SAMPLE]
    print(f"[样本模式] {_label} | 回测区间 {START} ~ {END}")
    if SAMPLE == "out":
        print("!! 期末考试提示：样本外区间只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
    engine.run_backtest_boll(
        SYMBOL_KEY, period=BOLL_PERIOD, k=BOLL_K, volume=VOLUME,
        start=START, end=END, eval_start=EVAL_START,
        init_balance=INIT_BALANCE, kline_sec=KLINE_SEC,
        strategy_name="布林带", open_html=WEB_GUI, sample_tag=SAMPLE,
        slip_ticks=1,               # 1 跳滑点（与 01 同口径，折进 TqSim 手续费）
        atr_period=ATR_PERIOD,
        atr_stop_mult=ATR_STOP_MULT,
        risk_pct=RISK_PCT,
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
        time_stop_bars=TIME_STOP_BARS,
    )

    # 回测跑完 -> 自动刷新「修改记录汇总报告」（03_布林带 账本）
    try:
        sys.path.insert(0, os.path.join(_ROOT, "工具"))
        import 汇总报告
        汇总报告.更新(SYMBOL_KEY, "布林带", SAMPLE)
    except Exception as _e:
        print(f"[汇总] 刷新失败（不影响回测结果）：{_e}")
