# -*- coding: utf-8 -*-
# ⚠️ 本文件由「PVC」版机械复制生成（2026-09-27 批量补品种，12 品种口径统一）。
#    品种相关处（SYMBOL_KEY / 报告路径 / 账本路径 / 滑点说明）已换成本品种；
#    docstring 里的版本沿革与实验数字记录的是**原品种「PVC」**的过程，本品种未单独复现。
#    核心买卖逻辑在 lib/engine.py，各品种共用同一份，不会走偏。
"""
摆动线策略 —— 回测入口（30分钟 · 摆动点结构双向反手，零参数裸版）
==========================================================
来源：文华麦语言自编指标「移动止损止盈线」（2022-05-14）1:1 转化。
核心共用 lib/engine.swing_decision + _swing_update_levels。

策略逻辑：
  · 摆动高点：连续 2 根更低高点 -> 前第 2 根高点确认；摆动低点镜像（滞后 2 根，防重绘）
  · 开多：收盘上穿最近确认摆动高点（下一根开盘执行）；持空前则同时平空反手
  · 开空：收盘跌破最近确认摆动低点（下一根开盘执行）；持多则同时平多反手
  · 移动止盈线：多头=最近确认摆动低点，空头=最近确认摆动高点，只前进不后退
  · 出场与反手是同一事件（结构反向穿越），不另设止损；零参数、零过滤裸版

怎么用：
  1. 改下面「可调参数」
  2. 双击同目录 回测.bat
  3. 浏览器自动出报告（资金曲线 + 逐笔成交 + 指标卡片）

注：摆动点只需 3 根已收盘 K 线即可开始确认，START 前移 1 周足够预热；
EVAL_START 之前只预热（状态机照常累积）、不交易。
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
SYMBOL_KEY = "JD"
VOLUME = 1                            # 每次开仓手数（新手先 1 手）
# ---- 样本内(练习) / 样本外(期末考试) 切分（规则3：样本外仅用于最终验证，看过一次即作废）----
# 练习(样本内)     = 2026-03-13 ~ 2026-09-13   一直用来开发/调参的那段，可反复跑
# 期末考试(样本外) = 2025-03-13 ~ 2025-09-13   去年同期，从没看过，只跑一次
# 切分由用户 2026-09-17 决定（"今年3-9是练习、去年3-9是期末考试"），定稿后不得再改。
IS_START  = date(2026, 3, 13)   # 练习(样本内)开始
IS_END    = date(2026, 9, 13)   # 练习(样本内)结束
OOS_START = date(2025, 3, 13)   # 期末考试(样本外)开始
OOS_END   = date(2025, 9, 13)   # 期末考试(样本外)结束
SAMPLE = "in"                   # "in"=练习(可反复跑) | "out"=期末考试(只跑一次)
assert SAMPLE in ("in", "out"), f"SAMPLE 只能填 'in' 或 'out'，现在是 {SAMPLE!r}"
EVAL_START = IS_START if SAMPLE == "in" else OOS_START
EVAL_END   = IS_END   if SAMPLE == "in" else OOS_END
START = EVAL_START - timedelta(days=7)   # 预热：摆动点确认只需 3 根，前移 1 周足够
END   = EVAL_END
INIT_BALANCE = 200000                # 初始资金（元）
KLINE_SEC = 30 * 60                     # 30 分钟 K 线（与 01/02 对齐）
WEB_GUI = True                        # 跑完用浏览器打开报告
# ---- v1（2026-09-23）：波动率预算开仓（与 01/03 同一把尺子）----
#   手数 = 权益 × RISK_PCT% ÷ (2.0×ATR×乘数)，四舍五入后夹在 [1, 8] 手
#   ⚠️ 分母的 2.0×ATR 只是「风险刻度」（保证 1~8 手量级与 01/03 一致），
#      不是止损线 —— 摆动线的出场仍是结构反向穿越，本策略不叠止损。
RISK_PCT = 1.0                        # 单笔目标风险占权益百分比（0=关闭，回退固定 VOLUME 手）
RISK_HAND_MIN = 1                     # 手数下限
RISK_HAND_MAX = 8                     # 手数上限
# ---- v2（2026-09-23）：固定止损（方案A）----
#   开仓时按 信号根收盘价 ∓ STOP_ATR×信号根ATR 定死止损线，不移动不上移。
#   触发后【空仓等下一次结构翻转】：状态机照常记忆，不反手、不回补；
#   触发当根若恰好结构翻转，视为新事件、允许开新方向（止损只管出场，入场仍归结构）。
STOP_ATR = 2.0                        # 0=关闭（回退 v1 行为）
# =================================================


if __name__ == "__main__":
    _label = {"in": "练习(样本内，可反复跑)", "out": "期末考试(样本外，只跑一次)"}[SAMPLE]
    print(f"[样本模式] {_label} | 回测区间 {START} ~ {END}")
    if SAMPLE == "out":
        print("!! 期末考试提示：样本外区间只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
    engine.run_backtest_swing(
        SYMBOL_KEY, volume=VOLUME,
        start=START, end=END, eval_start=EVAL_START,
        init_balance=INIT_BALANCE, kline_sec=KLINE_SEC,
        strategy_name="摆动线", open_html=WEB_GUI, sample_tag=SAMPLE,
        risk_pct=RISK_PCT, risk_hand_min=RISK_HAND_MIN,
        risk_hand_max=RISK_HAND_MAX,
        stop_atr=STOP_ATR,
    )

    # 回测跑完 -> 自动刷新「修改记录汇总报告」
    #   文字说明在 工具/_账本/鸡蛋_摆动线.json（人工维护）
    #   数字取自刚生成的报告，自动写入并重渲染 回测结果/05_摆动线/鸡蛋/鸡蛋_摆动线_汇总.html
    try:
        sys.path.insert(0, os.path.join(_ROOT, "工具"))
        import 汇总报告
        汇总报告.更新(SYMBOL_KEY, "摆动线", SAMPLE)
    except Exception as _e:
        print(f"[汇总] 刷新失败（不影响回测结果）：{_e}")
