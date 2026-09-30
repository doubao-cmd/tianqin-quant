# -*- coding: utf-8 -*-
"""
双均线策略 —— 回测入口（开盘保护实验版）
==========================================================
本脚本 = 2026-09-21 的 v6 基准（PVC_双均线_回测.py）逐字复制，
**只新增一个变量**：no_open_window_min=30。

实验目的（用户 2026-09-21 临时加的小条件，纯测试）：
  开盘后 30 分钟内不开新仓（日盘 09:00、夜盘 21:00 开盘），
  避开开盘瞬间的流动性差 / 跳空乱序，看这 30 分钟屏蔽会不会改善样本内表现。

其余一切参数（周期/均线/波动率预算/止损/滑点/样本窗）与 v6 完全一致，
保证这是「单变量」改动（规则4），结果可与 v6 基准 +1.80% 直接对比。

⚠️ 这是测试，不是定稿。若样本内改善且无其他副作用，再决定是否并入主脚本。
   跑完看数字，不要只看收益率——看「被拦下的开仓次数 + 跳过的是亏单还是赚单」。

真正的买卖逻辑在 lib/engine.py 的 trend_reverse_signal() 里（回测和模拟盘共用）。
"""
import sys
import os

# 把项目根目录（天勤量化/）加到搜索路径，import lib / config 才找得到
_ROOT = os.path.dirname(os.path.abspath(__file__))  # 从脚本所在目录开始
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)  # 逐级向上，找到含 lib 的项目根目录为止
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from datetime import date, timedelta
from lib import engine

# ==================== 可调参数 ====================
SYMBOL_KEY = "V"
N1 = 5                     # 快线周期
N2 = 20                    # 慢线周期
N3 = 60                    # 趋势过滤线 MA60：决定「能不能开反向仓」
VOLUME = 1                 # 固定手数（仅当 TARGET_RISK_PCT = 0 时生效）
# ---- v6 波动率预算（2026-09-19）----
TARGET_RISK_PCT = 1.0      # 1.0 = 每笔目标风险 1%（= 2000 元）；0 = 关闭，退回 VOLUME 手
HAND_MIN = 1               # 手数下限
HAND_MAX = 8               # 手数上限
# ---- 样本内(练习) / 样本外(期末考试) 切分 ----
IS_START  = date(2026, 3, 13)   # 练习(样本内)开始
IS_END    = date(2026, 9, 13)   # 练习(样本内)结束
OOS_START = date(2025, 3, 13)   # 期末考试(样本外)开始
OOS_END   = date(2025, 9, 13)   # 期末考试(样本外)结束
SAMPLE = "in"                   # "in"=样本内(开发用，可反复跑) | "out"=样本外(最终验证，只跑一次)
assert SAMPLE in ("in", "out"), f"SAMPLE 只能填 'in' 或 'out'，现在是 {SAMPLE!r}"
# 评估区间（实际交易窗口）与回测起点分离：回测起点前移 1 周做均线预热，预热期不交易
EVAL_START = IS_START if SAMPLE == "in" else OOS_START
EVAL_END   = IS_END   if SAMPLE == "in" else OOS_END
START = EVAL_START - timedelta(days=7)   # 预热：MA 需要历史，评估区间前 1 周不交易
END   = EVAL_END
INIT_BALANCE = 200000      # 初始资金（元）
KLINE_SEC = 30 * 60        # 30分钟
WEB_GUI = True             # 是否用浏览器打开图形化回测报告
# ---- 止损（2026-09-18 v4 · 阶段A：初始 ATR 止损 2×ATR，保本关闭）----
ATR_STOP_MULT = 2.0        # 初始止损：2×ATR(14)；0 = 关闭
BREAKEVEN_ATR = 0.0        # 保本触发：0 = 关闭（v4 回滚到阶段A）
ATR_PERIOD = 14            # ATR 周期
# =================================================

# ===== 开盘保护（本次实验唯一变量；2026-09-21 用户临时加的测试条件）=====
# 开盘后 NO_OPEN_WINDOW_MIN 分钟内不开新仓（日盘 09:00 / 夜盘 21:00 开盘）。
# 0 = 关闭（回到 v6 基准行为）。本实验设 30。
NO_OPEN_WINDOW_MIN = 30


if __name__ == "__main__":
    _label = {"in": "练习(样本内，可反复跑)", "out": "期末考试(样本外，只跑一次)"}[SAMPLE]
    print(f"[样本模式] {_label} | 回测区间 {START} ~ {END}")
    print(f"[开盘保护] 开盘后 {NO_OPEN_WINDOW_MIN} 分钟内不开新仓"
          f"（{'关闭' if NO_OPEN_WINDOW_MIN == 0 else '生效'}）")
    if SAMPLE == "out":
        print("!! 期末考试提示：样本外区间只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
    engine.run_backtest(
        SYMBOL_KEY, engine.trend_reverse_signal, N1, N2, VOLUME,
        START, END, INIT_BALANCE, KLINE_SEC,
        strategy_name="双均线", open_html=True, eval_start=EVAL_START,
        sample_tag=SAMPLE,
        pass_dir=True,     # 目标仓位式：允许「只平仓、不反手」（0 = 空仓等待）
        ma_filter=N3,      # 多算一条 MA60，作为趋势过滤
        slip_ticks=1,      # 1 跳滑点（PVC 1 跳 = 1点 × 5元 = 5 元/手/单边）
        atr_stop_mult=ATR_STOP_MULT,   # v4：初始 ATR 止损 2×ATR（阶段A）
        breakeven_atr=BREAKEVEN_ATR,   # v4：保本关闭 = 0
        atr_period=ATR_PERIOD,
        # v6(2026-09-19)：波动率预算手数
        risk_pct=TARGET_RISK_PCT,
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
        # ===== 本次实验唯一新增变量 =====
        no_open_window_min=NO_OPEN_WINDOW_MIN,
    )

    # 回测跑完 -> 自动刷新「修改记录汇总报告」
    try:
        sys.path.insert(0, os.path.join(_ROOT, "工具"))
        import 汇总报告
        汇总报告.更新(SYMBOL_KEY, "双均线", SAMPLE)
    except Exception as _e:
        print(f"[汇总] 刷新失败（不影响回测结果）：{_e}")
