# -*- coding: utf-8 -*-
"""
均值回归策略 —— 回测入口
==========================================================
怎么用：
  1. 改下面「可调参数」里的东西（品种、均值周期、样本选择）
  2. 双击同目录的  回测.bat
  3. 浏览器自动出报告（K线 + 买卖点 + 资金曲线）
  4. 同时自动刷新「修改记录汇总报告」： 回测结果/03_均值回归/苯乙烯/苯乙烯_均值回归_汇总.html
     （每次改了什么、为什么，写在 工具/_账本/苯乙烯_均值回归.json，数字自动抓）

真正的买卖逻辑在 lib/engine.py 的 rev_signal() 里（回测和模拟盘共用同一份）。

★ 本策略的买卖规则（三行讲完，2026-09-27 定）：
      收盘价 < MA(N1)  -> 目标做多（价格低于均值，赌它涨回去）
      收盘价 > MA(N1)  -> 目标做空（价格高于均值，赌它跌回去）
      穿越均线的瞬间即反手 —— **永远在场**，没有空仓状态
   参数只有 N1 一个：没有 σ 通道、没有中性区、没有任何过滤。

⚠️ 与 03_布林带（已收档）不是一回事，别搞混：
     布林带 = MA ± k×σ 两条轨道 + 中轨出场，要 std、要 k、要出场阈值；
     本策略 = 单均线穿越，穿越点就是反手点。同属均值回归家族，但不是一个东西。
⚠️ 已知代价（跑之前就该有数）：换手 = 均线穿越次数，是本项目所有策略里最高的，
   成本会吃掉相当一部分毛利。这一轮要回答的就是「裸逻辑还剩多少 edge」。
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
SYMBOL_KEY = "EB"
N1 = 20                    # 均值周期 MA(20) —— 本策略**唯一的参数**。
                           # 20 是常用先验值，定死不动、不扫描（规则2：不遍历参数择优）。
N2 = 20                    # signal_kind="rev" 用不到第二条均线，占位保持函数签名一致。
VOLUME = 1                 # 固定手数（仅当 TARGET_RISK_PCT = 0 时生效）
# ---- 仓位：波动率预算（与 01/02/04/05 完全同口径，横向对比才成立）----
# 单笔目标风险 = 权益 × TARGET_RISK_PCT，手数 = 该金额 ÷ (止损宽度 × 合约乘数)。
# 全部 12 品种统一这一套参数；手数按各品种自己的 ATR 与合约乘数自动缩放。
# 公式与边界处理见 lib/engine.py 的 _risk_budget_hand()。
TARGET_RISK_PCT = 1.0      # 1.0 = 每笔目标风险 1%（= 2000 元）；0 = 关闭，退回 VOLUME 手
HAND_MIN = 1               # 手数下限
HAND_MAX = 8               # 手数上限
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
EVAL_START = {"in": IS_START, "val": VAL_START, "out": OOS_START}[SAMPLE]
EVAL_END   = {"in": IS_END,   "val": VAL_END,   "out": OOS_END}[SAMPLE]
START = EVAL_START - timedelta(days=7)   # 预热：MA 需要历史，评估区间前 1 周不交易
END   = EVAL_END
INIT_BALANCE = 200000      # 初始资金（元）
KLINE_SEC = 30 * 60        # 30分钟（与其余四策略统一）
WEB_GUI = True             # 是否用浏览器打开图形化回测报告
# ---- 止损（与 01/02/04/05 完全同口径：初始 ATR 止损 2×ATR，保本关闭）----
# ⚠️ 这条止损**不是均值回归逻辑的一部分**，加它是为了让本策略与其余四个策略
#    在**同一把尺子**下比较。若只想看裸逻辑本身，把 ATR_STOP_MULT 改成 0。
ATR_STOP_MULT = 2.0        # 初始止损：2×ATR(14)；0 = 关闭
BREAKEVEN_ATR = 0.0        # 保本触发：0 = 关闭
ATR_PERIOD = 14            # ATR 周期
# ---- 开盘保护（与 01 v7 同口径）----
# ⚠️ 同上：这是既有定稿口径，不是为均值回归新加的过滤条件。想看裸逻辑设 0。
NO_OPEN_WINDOW_MIN = 30
# =================================================


if __name__ == "__main__":
    _label = {"in": "练习(样本内，可反复跑)", "val": "验证集(结构选择，可跑有限几次)", "out": "期末考试(样本外，只跑一次)"}[SAMPLE]
    print(f"[样本模式] {_label} | 回测区间 {START} ~ {END}")
    if SAMPLE == "out":
        print("!! 期末考试提示：样本外区间只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
    engine.run_backtest(
        SYMBOL_KEY, engine.rev_signal, N1, N2, VOLUME,
        START, END, INIT_BALANCE, KLINE_SEC,
        strategy_name="均值回归", open_html=True, eval_start=EVAL_START,
        sample_tag=SAMPLE,
        pass_dir=True,      # 目标仓位式（signal_kind="rev" 必须用这个）
        signal_kind="rev",  # ★ 信号值改从 rev_signal 来：单均线穿越即反手
        slip_ticks=1,       # v1(2026-09-27)：加 1 跳滑点（苯乙烯 1 跳 = 1点 × 5元 = 5 元/手/单边）
        atr_stop_mult=ATR_STOP_MULT,   # 与其余四策略同口径
        breakeven_atr=BREAKEVEN_ATR,
        atr_period=ATR_PERIOD,
        risk_pct=TARGET_RISK_PCT,      # 波动率预算手数（与其余四策略同口径）
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
        no_open_window_min=NO_OPEN_WINDOW_MIN,
    )

    # 回测跑完 -> 自动刷新「修改记录汇总报告」
    #   文字说明在 工具/_账本/苯乙烯_均值回归.json（人工维护）
    #   数字取自刚生成的报告，自动写入并重渲染 回测结果/03_均值回归/苯乙烯/苯乙烯_均值回归_汇总.html
    try:
        sys.path.insert(0, os.path.join(_ROOT, "工具"))
        import 汇总报告
        汇总报告.更新(SYMBOL_KEY, "均值回归", SAMPLE)
    except Exception as _e:
        print(f"[汇总] 刷新失败（不影响回测结果）：{_e}")
