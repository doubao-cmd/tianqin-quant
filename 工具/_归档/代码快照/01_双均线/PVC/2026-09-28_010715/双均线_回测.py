# -*- coding: utf-8 -*-
"""
双均线策略 —— 回测入口
==========================================================
怎么用：
  1. 改下面「可调参数」里的东西（品种、周期、手数、样本选择）
  2. 双击同目录的  回测.bat
  3. 浏览器自动出报告（K线 + 买卖点 + 资金曲线）
  4. 同时自动刷新「修改记录汇总报告」： 回测结果/01_双均线/PVC/PVC_双均线_汇总.html
     （每次改了什么、为什么，写在 工具/_账本/PVC_双均线.json，数字自动抓）

真正的买卖逻辑在 lib/engine.py 的 trend_reverse_signal() 里（回测和模拟盘共用同一份）。
想换一套买卖规则，只改那一个函数就行。
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
# 单笔目标风险 = 权益 × TARGET_RISK_PCT，手数 = 该金额 ÷ (止损宽度 × 合约乘数)。
# ⚠️ 本次只改「手数」：不加移动止损 / 不加分级平仓 / 不加保本；
#    信号、周期、均线、止损线、滑点全部不动（规则4：每次只改一处逻辑）。
# 公式与边界处理见 lib/engine.py 的 _risk_budget_hand()；
# 20 万账户的逐项压测（保证金/跳空/连亏/手数上限敏感性）见
# 工具/_输出/_波动率预算结论_0919.txt。
TARGET_RISK_PCT = 1.0      # 1.0 = 每笔目标风险 1%（= 2000 元）；0 = 关闭，退回 VOLUME 手
HAND_MIN = 1               # 手数下限
HAND_MAX = 8               # 手数上限：修正低波动期 ATR 低估（约 1.22 倍），
                           # 并把「单日 5% 极端跳空」的亏损压到账户约 5.1%
# ---- 样本内(练习) / 样本外(期末考试) 切分（规则3：样本外仅用于最终验证，看过一次即作废）----
# 练习(样本内)     = 2016-01-01 ~ 2019-12-31   4 年；2026-09-27 用户拍板重定，历史从未跑过
# 验证集           = 2020-01-01 ~ 2024-12-31   5 年；2020 从未跑过，2021-2024 原是跨年矩阵四窗
# 期末考试(样本外) = 2025-01-01 ~ 2026-09-30   21 个月；⚠️ 其中 2025-03~09 曾被 01 用作 OOS
# 样本外区间不得改动、不得提前查看。
IS_START  = date(2016, 1, 1)   # 练习(样本内)开始
IS_END    = date(2019, 12, 31)   # 练习(样本内)结束
OOS_START = date(2025, 1, 1)   # 期末考试(样本外)开始
OOS_END   = date(2026, 9, 30)   # 期末考试(样本外)结束
# ---- 验证集（2026-09-27 新增）：结构选择用，可跑有限几次，不用于最终验证 ----
# 2020-2024 中 2020 从未跑过；2021-2024-25 原是「跨年矩阵」的四个窗口。
VAL_START = date(2020, 1, 1)
VAL_END   = date(2024, 12, 31)
SAMPLE = "in"                   # "in"=样本内 | "val"=验证集 | "out"=期末考试(只看一次)
assert SAMPLE in ("in", "val", "out"), f"SAMPLE 只能填 'in'/'val'/'out'，现在是 {SAMPLE!r}"
# 评估区间（实际交易窗口）与回测起点分离：回测起点前移 1 周做均线预热，预热期不交易
EVAL_START = {"in": IS_START, "val": VAL_START, "out": OOS_START}[SAMPLE]
EVAL_END   = {"in": IS_END,   "val": VAL_END,   "out": OOS_END}[SAMPLE]
START = EVAL_START - timedelta(days=7)   # 预热：MA 需要历史，评估区间前 1 周不交易
END   = EVAL_END
INIT_BALANCE = 200000      # 初始资金（元）
KLINE_SEC = 30 * 60        # 30分钟（v5 2026-09-18：5分钟 → 30分钟；均线仍 5/20/60，用户指定不同步放大）
WEB_GUI = True             # 是否用浏览器打开图形化回测报告
# ---- 止损（2026-09-18 v4 · 回滚到阶段A：初始 ATR 止损 2×ATR，保本关闭）----
# 阶段A：开仓那一刻锁定 entry_atr，止损线 = 开仓价 ∓ 2×ATR(14)，触发即平仓，
# 优先于均线信号；持仓期间 ATR 不漂移，不含保本/移动/追踪。
# 它是三次出场实验里唯一净改善的一档（旧短窗 -0.23% → -0.16%，止损 16/40）：
# 作用是「给单笔亏损封顶」，不能消灭浮盈回吐（触发时那笔仍亏约 2×ATR）。
# 阶段B 的保本（浮盈 1.5×ATR 后移到开仓价）本次关闭（=0）：
# 它把「小亏」变「零亏」，但把还能走的单子砍成零亏，净额打平略负。
# 倍数 2.0 直接沿用阶段A 当时的取值，不重扫（规则2：不遍历参数择优）。
ATR_STOP_MULT = 2.0        # 初始止损：2×ATR(14)；0 = 关闭
BREAKEVEN_ATR = 0.0        # 保本触发：0 = 关闭（v4 回滚到阶段A）
ATR_PERIOD = 14            # ATR 周期
# ---- v7 开盘保护（2026-09-21）----
# 开盘后前 30 分钟内禁止「空仓开新仓」（日盘 09:00 / 夜盘 21:00 起）；
# 已有持仓的平仓/反手不受影响。0 = 关闭（v6 行为）。
# 五窗口 A/B（收益率）：2026IS +4.12pp｜2023 +4.57｜2022 +3.14｜2024-25 -2.33｜2021 -2.64
# 净 +6.86pp（3正2负）；开仓 287→235 笔（-18%）。详见 账本 v7。
NO_OPEN_WINDOW_MIN = 30
# =================================================


# 买卖逻辑在 lib/engine.py 的 trend_reverse_signal()，回测与模拟盘共用同一份，两侧不会走偏。
# 规则（MA5 / MA20 / MA60，全部取「已收盘」那根 K 线，下一根开盘成交）：
#
#   金叉 且 MA20 >  MA60 -> 目标做多（持空则反手做多）
#   金叉 且 MA20 <= MA60 -> 只平空、空仓等待（不反手）
#   死叉 且 MA20 <  MA60 -> 目标做空（持多则反手做空）
#   死叉 且 MA20 >= MA60 -> 只平多、空仓等待（不反手）
#   无交叉               -> 维持当前持仓方向
#
# 一句话：交叉只决定「平不平」，MA20 与 MA60 的关系决定「能不能反向开仓」。
# 要改买卖规则，只改 engine.py 里那一个函数即可。
#
# 【v4】止损（阶段A：初始 ATR 止损）：
#   开仓那一刻锁定 entry_atr，止损线 = 开仓价 ∓ 2×ATR(14)；
#   价格触及即平仓，优先于均线信号（一旦打掉，当根不再看金叉/死叉）。
#   持仓期间 ATR 不漂移；不含保本 / 移动 / 追踪止损。
#
# 【v6】仓位（波动率预算）：
#   开仓那一刻用「信号根已收盘那根的 ATR」算手数，持仓期间不变（不加仓、不递减）：
#       手数 = round( 权益 × TARGET_RISK_PCT ÷ (ATR_STOP_MULT × ATR × 合约乘数) )
#   与止损线共用同一个 ATR —— 止损线在 开仓价 ∓ 2×ATR 处，所以
#   「被止损打掉时亏多少」正好 ≈ 目标风险额，风险预算与实际风险同一把尺子。
#   ⚠️ 这是纯杠杆刻度：收益率/回撤按同一倍数放大，夏普/卡玛不会因此改善。
#      判据看夏普 / 卡玛 / 回撤 + R，不要看收益率。
#   ⚠️ 加仓成本假设是按 1 手做的（结论15/20），多手后的冲击成本回测模拟不出来，
#      须以模拟盘实测为准。


if __name__ == "__main__":
    _label = {"in": "练习(样本内，可反复跑)", "val": "验证集(结构选择，可跑有限几次)", "out": "期末考试(样本外，只跑一次)"}[SAMPLE]
    print(f"[样本模式] {_label} | 回测区间 {START} ~ {END}")
    if SAMPLE == "out":
        print("!! 期末考试提示：样本外区间只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
    engine.run_backtest(
        SYMBOL_KEY, engine.trend_reverse_signal, N1, N2, VOLUME,
        START, END, INIT_BALANCE, KLINE_SEC,
        strategy_name="双均线", open_html=True, eval_start=EVAL_START,
        sample_tag=SAMPLE,
        pass_dir=True,     # 目标仓位式：允许「只平仓、不反手」（0 = 空仓等待）
        ma_filter=N3,      # 多算一条 MA60，作为趋势过滤
        slip_ticks=1,      # v1(2026-09-17)：加 1 跳滑点（PVC 1 跳 = 1点 × 5元 = 5 元/手/单边）
                           # 注意：没传 commission_per_hand -> 手续费仍是 TqSim 交易所默认，
                           # 本次只引入「滑点」这一个变量，口径可归因。
        atr_stop_mult=ATR_STOP_MULT,   # v4(2026-09-18)：初始 ATR 止损 2×ATR（阶段A）
        breakeven_atr=BREAKEVEN_ATR,   # v4(2026-09-18)：保本关闭 = 0
        atr_period=ATR_PERIOD,
        # v6(2026-09-19)：波动率预算手数 —— 唯一的改动点。
        # 手数 = 权益×TARGET_RISK_PCT ÷ (ATR_STOP_MULT×信号根ATR×乘数)，夹在 [HAND_MIN, HAND_MAX]。
        # TARGET_RISK_PCT=0 时完全不生效，退回 VOLUME 手（其余 4 品种仍走这条老路）。
        risk_pct=TARGET_RISK_PCT,
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
        no_open_window_min=NO_OPEN_WINDOW_MIN,  # v7(2026-09-21)：开盘后30分钟不开新仓
    )

    # 回测跑完 -> 自动刷新「修改记录汇总报告」
    #   文字说明在 工具/_账本/PVC_双均线.json（人工维护）
    #   数字取自刚生成的报告，自动写入并重渲染 回测结果/01_双均线/PVC/PVC_双均线_汇总.html
    try:
        sys.path.insert(0, os.path.join(_ROOT, "工具"))
        import 汇总报告
        汇总报告.更新(SYMBOL_KEY, "双均线", SAMPLE)
    except Exception as _e:
        print(f"[汇总] 刷新失败（不影响回测结果）：{_e}")
