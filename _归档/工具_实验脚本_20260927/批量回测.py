# -*- coding: utf-8 -*-
"""
批量回测 —— 一次跑完「3 策略 × 5 品种」
=========================================================================
01 双均线(5分钟) + 02 均线排列(15分钟) + 03 布林带(5分钟)，各 5 个品种
（PVC/甲醇/纯碱/螺纹钢/豆粕）

用法：双击同目录 批量回测.bat
     只想跑某个策略：python 工具/批量回测.py --only 03
输出：报告 → 回测结果/<策略>/<品种>/<品种>_<策略>_<周期>[_in|_out].html
      明细 → 回测结果/<策略>/<品种>/<品种>_<策略>_<周期>[_in|_out].txt
      （<周期> 自动带出，如 5min / 15min）
      日志 → 运行日志/批量_回测.log

样本口径（用户 2026-09-17 拍板，定稿不改）：
  练习(样本内)     = 2026-03-13 ~ 2026-09-13   一直用来开发/调参，可反复跑
  期末考试(样本外) = 2025-03-13 ~ 2025-09-13   去年同期，从没看过，**只跑一次**

⛔ 纪律（规则3）：SAMPLE="out" 跑完无论结果好坏，都不得据此修改策略；
   一旦看了样本外又拿它做决策，这段就被污染、必须另换新区间。
=========================================================================
"""
import sys
import os
import time
import traceback
from datetime import date, timedelta

# 向上找到含 lib 的项目根目录
_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ==================== 可调参数 ====================
SAMPLE = "in"           # "in"=练习(样本内，可反复跑) | "out"=期末考试(样本外，只跑一次)
OPEN_REPORT = False     # True=每个品种跑完自动弹浏览器（会连开 10 个窗口，建议 False）
SYMS = [("V", "PVC"), ("MA", "甲醇"), ("SA", "纯碱"), ("RB", "螺纹钢"), ("M", "豆粕")]
INIT_BALANCE = 200000   # 初始资金（元）
# ================================================

IS_START, IS_END = date(2026, 3, 13), date(2026, 9, 13)
OOS_START, OOS_END = date(2025, 3, 13), date(2025, 9, 13)
assert SAMPLE in ("in", "out"), f'SAMPLE 只能填 "in" 或 "out"，现在是 {SAMPLE!r}'

EVAL_START = IS_START if SAMPLE == "in" else OOS_START
EVAL_END = IS_END if SAMPLE == "in" else OOS_END
START = EVAL_START - timedelta(days=7)   # 预热：起点前移 1 周，评估区间前不交易
END = EVAL_END


def cross_signal(now_f, now_s, pre_f, pre_s):
    """01 双均线核心逻辑（与 双均线_回测.py 中的定义逐字一致）"""
    if pre_f <= pre_s and now_f > now_s:
        return 1
    if pre_f >= pre_s and now_f < now_s:
        return -1
    return 0


jobs = []
for _k, _cn in SYMS:
    jobs.append((
        "01_双均线", _cn, _k,
        lambda k=_k: engine.run_backtest(
            k, cross_signal, 5, 20, 1, START, END, INIT_BALANCE, 5 * 60,
            strategy_name="双均线", open_html=OPEN_REPORT, eval_start=EVAL_START,
            sample_tag=SAMPLE),
    ))
for _k, _cn in SYMS:
    jobs.append((
        "02_均线排列", _cn, _k,
        lambda k=_k: engine.run_backtest_align(
            k, engine.align_signal_long_macd, (5, 10, 20, 40), 1, START, END,
            eval_start=EVAL_START, init_balance=INIT_BALANCE, kline_sec=15 * 60,
            strategy_name="均线排列", open_html=OPEN_REPORT, stop_pct=0.0,
            sample_tag=SAMPLE),
    ))
for _k, _cn in SYMS:
    jobs.append((
        "03_布林带", _cn, _k,
        lambda k=_k: engine.run_backtest_boll(
            k, period=20, k=2.0, stop_pct=0.02, volume=1, start=START, end=END,
            eval_start=EVAL_START, init_balance=INIT_BALANCE, kline_sec=5 * 60,
            strategy_name="布林带", open_html=OPEN_REPORT, sample_tag=SAMPLE),
    ))

# --only 03 / --only 布林带：只跑匹配的策略（省时间）
_only = ""
for _a in sys.argv[1:]:
    if _a == "--only":
        _i = sys.argv.index(_a)
        _only = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else ""
if _only:
    jobs = [j for j in jobs if _only in j[0]]

_label = {"in": "练习(样本内，可反复跑)", "out": "期末考试(样本外，只跑一次)"}[SAMPLE]
print("=" * 72)
print(f"[样本模式] {_label}")
print(f"[回测区间] {START} ~ {END}（评估区间 {EVAL_START} 起，前 1 周仅预热不交易）")
print(f"[任务数量] {len(jobs)} 个" + (f"（已按 --only {_only} 过滤）" if _only else " = 3 策略 × 5 品种"))
if not jobs:
    print("!! 过滤后没有任务，--only 传错了？可选：01 / 02 / 03")
    sys.exit(1)
if SAMPLE == "out":
    print("!! 期末考试提示：样本外区间只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
print("=" * 72)

summary = []
t_all = time.time()
for i, (st, cn, k, fn) in enumerate(jobs, 1):
    print("")
    print("-" * 72)
    print(f"[{i}/{len(jobs)}] {st} / {cn}({k})  开始 {time.strftime('%H:%M:%S')}")
    print("-" * 72)
    t0 = time.time()
    ok, err = True, ""
    try:
        fn()
    except Exception as e:
        ok = False
        err = f"{type(e).__name__}: {e}"
        print(f"  !! 该任务失败：{err}")
        traceback.print_exc()
    dur = time.time() - t0
    print(f"[{i}/{len(jobs)}] {'完成' if ok else '失败'}  用时 {dur:.0f}s")
    summary.append((st, cn, ok, dur, err))

print("")
print("=" * 72)
print("汇总")
print("=" * 72)
for st, cn, ok, dur, err in summary:
    print(f"  {st:<12s} {cn:<6s} {'OK  ' if ok else 'FAIL'} {dur:7.0f}s  {err}")
print(f"总计用时 {time.time() - t_all:.0f}s")
print("ALL_DONE")
